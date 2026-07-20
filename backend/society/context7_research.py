from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from typing import Any

from agno.run import RunContext
from agno.tools import tool

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import Settings


def _extract_library_hint(prompt: str) -> str | None:
    """Derive a Context7 library name hint from the task prompt.

    Scans for well-known library/framework identifiers so that non-Agno tasks
    resolve the relevant library instead of falsely asking Agno docs. Returns
    None when no recognizable hint is found, in which case the caller should
    let Context7 resolve-library-id pick the best match from the query alone.
    """

    _KNOWN_LIBRARIES: tuple[tuple[tuple[str, ...], str], ...] = (
        (("yjs", "y-js"), "yjs"),
        (("crdt",), "yjs"),
        (("automerge",), "automerge"),
        (("react",), "react"),
        (("next", "nextjs", "next.js"), "next.js"),
        (("vue", "vuejs", "vue.js"), "vue"),
        (("svelte",), "svelte"),
        (("django",), "django"),
        (("flask",), "flask"),
        (("fastapi",), "fastapi"),
        (("express",), "express"),
        (("nestjs", "nest.js"), "nestjs"),
        (("pydantic",), "pydantic"),
        (("sqlalchemy",), "sqlalchemy"),
        (("prisma",), "prisma"),
        (("tailwind",), "tailwindcss"),
        (("typescript",), "typescript"),
        (("rust",), "rust"),
        (("tokio",), "tokio"),
        (("serde",), "serde"),
        (("langchain",), "langchain"),
        (("llamaindex",), "llamaindex"),
        (("openai",), "openai"),
        (("anthropic",), "anthropic"),
        (("agno",), "agno"),
        (("mcp",), "modelcontextprotocol"),
    )
    lowered = prompt.lower()
    for triggers, lib_name in _KNOWN_LIBRARIES:
        for trigger in triggers:
            if trigger in lowered:
                return lib_name
    return None


def _build_context7_query(prompt: str) -> str:
    """Build a Context7 query that reflects the actual task instead of Agno."""

    return prompt.strip()[:2000] or "general documentation"


def _text_from_tool_result(result: Any) -> str:
    """Extract readable text from an MCP tool result."""

    content = getattr(result, "content", None)
    if not content:
        return str(result)
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts) or str(result)


def _first_library_id(text: str) -> str | None:
    """Return the first Context7 library id from resolver output."""

    match = re.search(r"Context7-compatible library ID:\s*(/[^\s]+)", text)
    return match.group(1) if match else None


async def collect_context7_evidence(
    settings: Settings,
    query: str,
    library_name: str | None = None,
) -> dict[str, Any]:
    """Collect Context7 evidence deterministically for researcher turns.

    This deliberately uses the MCP client directly instead of asking the model
    to decide whether to call Context7. The model receives the retrieved
    evidence afterwards and must reason from it.

    Args:
        settings: Application settings with MCP command configuration.
        query: The task prompt or researcher context to search for.
        library_name: Optional library name hint. When None, the function
            derives a hint from the query via ``_extract_library_hint``.
    """

    command_parts = shlex.split(settings.context7_mcp_command)
    if not command_parts:
        return {
            "success": False,
            "error": "CONTEXT7_MCP_COMMAND is empty.",
            "tool_calls": [],
        }

    resolved_library_name = library_name or _extract_library_hint(query) or "agno"
    context7_query = _build_context7_query(query)

    tool_calls: list[dict[str, Any]] = []
    params = StdioServerParameters(command=command_parts[0], args=command_parts[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            resolve_args = {
                "libraryName": resolved_library_name,
                "query": context7_query,
            }
            resolved = await session.call_tool("resolve-library-id", resolve_args)
            resolved_text = _text_from_tool_result(resolved)
            tool_calls.append({
                "tool_name": "resolve-library-id",
                "arguments": resolve_args,
                "result": resolved_text[:4000],
            })

            library_id = _first_library_id(resolved_text)
            if not library_id:
                library_id = f"/{resolved_library_name}/docs"
            docs_args = {
                "libraryId": library_id,
                "query": context7_query,
            }
            docs = await session.call_tool("query-docs", docs_args)
            docs_text = _text_from_tool_result(docs)
            tool_calls.append({
                "tool_name": "query-docs",
                "arguments": docs_args,
                "result": docs_text[:8000],
            })

    return {
        "success": True,
        "library_id": tool_calls[-1]["arguments"]["libraryId"],
        "tool_calls": tool_calls,
    }


def build_context7_lookup_tool(
    settings: Settings,
    *,
    max_calls: int = 2,
    on_intent: Callable[[dict[str, Any]], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Build a task-scoped Context7 tool selected autonomously by Researcher.

    The tool's required arguments form the durable intent record.  The intent
    callback runs before MCP/network activity, while the result callback
    records provenance after the lookup.  A closure-local budget prevents an
    agent from repeatedly searching without imposing keyword routing.
    """

    calls = 0

    @tool(name="context7_lookup")
    async def context7_lookup(
        objective: str,
        query: str,
        expected_evidence: str,
        library_name: str | None = None,
        run_context: RunContext | None = None,
    ) -> str:
        """Look up authoritative library documentation through Context7.

        Args:
            objective: Why external documentation is needed for this task.
            query: The precise documentation question to answer.
            expected_evidence: What a useful result should establish.
            library_name: Optional library or framework name to resolve.

        Returns:
            JSON containing Context7 result provenance or an honest failure.
        """

        nonlocal calls
        intent = {
            "objective": objective.strip(),
            "selected_tool": "context7_lookup",
            "query": query.strip(),
            "expected_evidence": expected_evidence.strip(),
            "library_name": library_name.strip() if library_name else None,
            "call_number": calls + 1,
        }
        if on_intent is not None:
            on_intent(intent)
        if calls >= max(1, max_calls):
            result = {
                "success": False,
                "error": "Context7 lookup budget exhausted for this researcher turn.",
                "intent": intent,
                "tool_calls": [],
            }
        else:
            calls += 1
            try:
                result = await collect_context7_evidence(settings, query, library_name)
            except Exception as exc:
                result = {
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "tool_calls": [],
                }
            result["intent"] = intent
        if run_context is not None:
            run_context.session_state.setdefault("research_evidence", []).append(result)
        if on_result is not None:
            on_result(result)
        return json.dumps(result, ensure_ascii=False)

    return context7_lookup


def format_context7_evidence(evidence: dict[str, Any]) -> str:
    """Format collected evidence for inclusion in a researcher prompt."""

    if not evidence.get("success"):
        return f"Context7 evidence collection failed: {evidence.get('error', 'unknown error')}"

    lines = [
        "Context7 evidence collected by the backend before this researcher turn.",
        f"Resolved library id: {evidence.get('library_id', 'unknown')}",
    ]
    for call in evidence.get("tool_calls", []):
        lines.append(f"\nTool: {call.get('tool_name')}")
        lines.append(f"Arguments: {call.get('arguments')}")
        lines.append(f"Result excerpt:\n{call.get('result', '')}")
    return "\n".join(lines)
