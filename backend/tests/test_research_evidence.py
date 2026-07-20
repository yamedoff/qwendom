from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.context7_research import (
    _build_context7_query,
    _extract_library_hint,
    build_context7_lookup_tool,
    collect_context7_evidence,
)
from config import Settings
from society.agents import role_tool_context
from society.models import AgentProfile, SocietyAgent


class Context7AutonomyToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_intent_before_lookup_and_provenance_after(self) -> None:
        timeline: list[str] = []

        async def fake_collect(settings, query, library_name=None):
            timeline.append("lookup")
            return {"success": True, "library_id": "/pydantic/docs", "tool_calls": []}

        context7_tool = build_context7_lookup_tool(
            MagicMock(),
            on_intent=lambda value: timeline.append(f"intent:{value['query']}"),
            on_result=lambda value: timeline.append(f"result:{value['success']}"),
        )
        with patch("society.context7_research.collect_context7_evidence", fake_collect):
            raw = await context7_tool.entrypoint(
                objective="Verify current validation behavior",
                query="Pydantic v2 model validators",
                expected_evidence="Official API semantics",
            )

        self.assertEqual(timeline, ["intent:Pydantic v2 model validators", "lookup", "result:True"])
        self.assertEqual(json.loads(raw)["intent"]["selected_tool"], "context7_lookup")

    async def test_enforces_turn_budget_without_second_lookup(self) -> None:
        collect = AsyncMock(return_value={"success": True, "tool_calls": []})
        context7_tool = build_context7_lookup_tool(MagicMock(), max_calls=1)
        kwargs = {
            "objective": "Verify docs",
            "query": "A precise documentation question",
            "expected_evidence": "An authoritative answer",
        }
        with patch("society.context7_research.collect_context7_evidence", collect):
            await context7_tool.entrypoint(**kwargs)
            second = json.loads(await context7_tool.entrypoint(**kwargs))

        collect.assert_awaited_once()
        self.assertFalse(second["success"])
        self.assertIn("budget exhausted", second["error"])


class Context7AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    """Tool availability is role-scoped, never selected by prompt keywords."""

    @staticmethod
    def _identity(agent_id: str) -> SocietyAgent:
        return SocietyAgent(
            id=agent_id,
            name=agent_id.title(),
            role="Research Analyst" if agent_id == "researcher" else "Systems Architect",
            skills=["evidence"],
            profile=AgentProfile(),
        )

    async def test_researcher_receives_optional_context7_tool(self) -> None:
        settings = Settings(
            LLM_PROVIDER="qwen",
            QWEN_API_KEY="test-key",
            ROLE_SPECIFIC_TOOLS_ENABLED=True,
            CONTEXT7_MCP_ENABLED=True,
        )
        async with role_tool_context(self._identity("researcher"), settings) as tools:
            self.assertEqual(len(tools), 1)

    async def test_unauthorized_role_cannot_receive_context7_tool(self) -> None:
        settings = Settings(
            LLM_PROVIDER="qwen",
            QWEN_API_KEY="test-key",
            ROLE_SPECIFIC_TOOLS_ENABLED=True,
            CONTEXT7_MCP_ENABLED=True,
        )
        async with role_tool_context(self._identity("architect"), settings) as tools:
            self.assertEqual(tools, [])


class ExtractLibraryHintTests(unittest.TestCase):
    def test_crdt_yjs_prompt_resolves_to_yjs(self) -> None:
        prompt = "Compare CRDT approaches using Yjs for real-time collaboration."
        self.assertEqual(_extract_library_hint(prompt), "yjs")

    def test_yjs_direct_mention_resolves_to_yjs(self) -> None:
        self.assertEqual(_extract_library_hint("How does Yjs handle awareness?"), "yjs")

    def test_agno_prompt_resolves_to_agno(self) -> None:
        self.assertEqual(_extract_library_hint("Set up Agno MCPTools"), "agno")

    def test_react_prompt_resolves_to_react(self) -> None:
        self.assertEqual(_extract_library_hint("React useEffect cleanup patterns"), "react")

    def test_ordinary_prompt_returns_none(self) -> None:
        self.assertIsNone(_extract_library_hint("Build a todo app with save functionality."))
        self.assertIsNone(_extract_library_hint("Refactor the login page."))

    def test_case_insensitive_matching(self) -> None:
        self.assertEqual(_extract_library_hint("Using YJS for collaboration"), "yjs")
        self.assertEqual(_extract_library_hint("AGNO framework setup"), "agno")


class BuildContext7QueryTests(unittest.TestCase):
    def test_uses_prompt_content_not_hardcoded_agno(self) -> None:
        prompt = "Compare CRDT vs centralized notes with cited evidence."
        query = _build_context7_query(prompt)
        self.assertIn("CRDT", query)
        self.assertNotIn("Agno Python MCPTools", query)

    def test_empty_prompt_returns_fallback(self) -> None:
        self.assertEqual(_build_context7_query(""), "general documentation")
        self.assertEqual(_build_context7_query("   "), "general documentation")


class CollectContext7EvidenceLibraryResolutionTests(unittest.TestCase):
    def test_crdt_prompt_resolves_yjs_not_agno(self) -> None:
        mock_settings = MagicMock()
        mock_settings.context7_mcp_command = "echo test"

        captured_args: dict = {}

        class FakeResolved:
            content = [MagicMock(text="Context7-compatible library ID: /yjs/yjs")]

        class FakeDocs:
            content = [MagicMock(text="Yjs documentation content")]

        async def fake_call_tool(tool_name, args):
            captured_args[tool_name] = dict(args)
            if tool_name == "resolve-library-id":
                return FakeResolved()
            return FakeDocs()

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = fake_call_tool

        class FakeClientSession:
            def __init__(self, r, w):
                pass
            async def __aenter__(self):
                return mock_session
            async def __aexit__(self, *a):
                pass

        class FakeStdioClient:
            def __init__(self, params):
                pass
            async def __aenter__(self):
                return (AsyncMock(), AsyncMock())
            async def __aexit__(self, *a):
                pass

        with patch("society.context7_research.ClientSession", FakeClientSession), \
             patch("society.context7_research.stdio_client", FakeStdioClient):
            result = asyncio.run(
                collect_context7_evidence(
                    mock_settings,
                    "Compare CRDT approaches using Yjs for real-time collaboration with cited evidence.",
                )
            )

        self.assertTrue(result["success"])
        resolve_args = captured_args.get("resolve-library-id", {})
        self.assertEqual(resolve_args["libraryName"], "yjs")
        self.assertIn("CRDT", resolve_args["query"])
        self.assertNotIn("Agno Python MCPTools", resolve_args["query"])

    def test_agno_prompt_still_resolves_agno(self) -> None:
        mock_settings = MagicMock()
        mock_settings.context7_mcp_command = "echo test"

        captured_args: dict = {}

        class FakeResolved:
            content = [MagicMock(text="Context7-compatible library ID: /agno-agi/docs")]

        class FakeDocs:
            content = [MagicMock(text="Agno documentation")]

        async def fake_call_tool(tool_name, args):
            captured_args[tool_name] = dict(args)
            if tool_name == "resolve-library-id":
                return FakeResolved()
            return FakeDocs()

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = fake_call_tool

        class FakeClientSession:
            def __init__(self, r, w):
                pass
            async def __aenter__(self):
                return mock_session
            async def __aexit__(self, *a):
                pass

        class FakeStdioClient:
            def __init__(self, params):
                pass
            async def __aenter__(self):
                return (AsyncMock(), AsyncMock())
            async def __aexit__(self, *a):
                pass

        with patch("society.context7_research.ClientSession", FakeClientSession), \
             patch("society.context7_research.stdio_client", FakeStdioClient):
            result = asyncio.run(
                collect_context7_evidence(
                    mock_settings,
                    "Set up Agno MCPTools for the agent with MCP server.",
                )
            )

        self.assertTrue(result["success"])
        resolve_args = captured_args.get("resolve-library-id", {})
        self.assertEqual(resolve_args["libraryName"], "agno")


if __name__ == "__main__":
    unittest.main()
