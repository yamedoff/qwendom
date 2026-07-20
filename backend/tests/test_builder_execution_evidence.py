"""Regression tests for builder execution-evidence gating (orchestrator.py ~L3373).

Verifies:
1. Analysis-only and generic implementation work do not use benchmark-only
   ``execute_notes_demo`` evidence.
2. Only an explicit collaborative-notes request may record that fixed demo
   evidence. Generic code execution remains truthful rather than accepting
   its ``server.py``/``client.html`` artifact pair.

No network or Qwen calls: all LLM/tool interactions are mocked.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.delegation import SubtaskAssignment, SubtaskReport


def _make_settings(**overrides: Any) -> Settings:
    defaults = {
        "LLM_PROVIDER": "qwen_legacy",
        "QWEN_LEGACY_API_KEY": "test-key",
        "QWEN_LEGACY_MODEL": "test-model",
        "LLM_TIMEOUT_SECONDS": 10,
        "READINESS_CONCURRENCY": 3,
        "SUBTASK_MAX_ATTEMPTS": 3,
        "SUBTASK_BACKOFF_BASE_SECONDS": 0.001,
        "SUBTASK_BACKOFF_CAP_SECONDS": 0.01,
        "DELEGATION_TOOLS_ENABLED": True,
        "EFFICIENT_SOCIETY_ENABLED": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_orchestrator(settings: Settings | None = None) -> SocietyOrchestrator:
    settings = settings or _make_settings()
    with patch("society.orchestrator.get_agno_db", return_value=None):
        with patch("society.orchestrator.get_settings", return_value=settings):
            orch = SocietyOrchestrator(settings=settings)
    return orch


def _make_agents(ids: list[str]) -> dict[str, SocietyAgent]:
    agents = {}
    for agent_id in ids:
        agents[agent_id] = SocietyAgent(
            id=agent_id,
            name=agent_id.capitalize(),
            role="Test Role",
            skills=["test"],
            profile=AgentProfile(
                values=["test"],
                communication_style="test",
                risk_tolerance="medium",
                decision_bias="test",
                default_blockers=[],
            ),
        )
    return agents


def _setup_task(
    orch: SocietyOrchestrator,
    roster: list[str],
    prompt: str,
    builder_subtask_id: str = "subtask-builder-001",
) -> tuple[TaskRun, Team, dict[str, Any]]:
    task = TaskRun(prompt=prompt, status="running")
    orch.tasks[task.id] = task
    team = Team(task_id=task.id, member_ids=list(roster), status="active")
    orch.teams[team.id] = team
    task.team_id = team.id
    builder_subtask = {
        "id": builder_subtask_id,
        "agent_id": "builder",
        "subtask": "Do the builder work",
        "status": "planned",
        "done_criteria": ["Done"],
        "why_assigned": "Test",
        "blocking_if_missing": False,
        "source": "test",
        "provenance": "test",
    }
    state = {
        "subtasks": [builder_subtask],
        "subtask_attempts": {},
        "subtask_idempotency_keys": {},
        "metrics": {"tool_calls": 0, "tool_calls_failed": 0, "governance_rounds": 0, "debate_rounds": 0},
        "phase": "delegation",
        "public_room": {
            "positions": [], "objections": [], "endorsements": [],
            "mind_changes": [], "published_private_notes": [],
            "collaboration_actions": [],
        },
    }
    orch.session_states[task.id] = state
    return task, team, state


class BuilderExecutionEvidenceGateTests(unittest.TestCase):

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def _install_mocks(
        self,
        orch: SocietyOrchestrator,
        work_result: str = "builder work product",
    ) -> dict[str, Any]:
        captured: dict[str, Any] = {"governance_calls": [], "report_called": False, "tool_calls": []}

        async def mock_governance_tool(
            task, actor_identity, tool_func, tool_name, schema_class, prompt,
            extra_instructions=None,
        ):
            captured["governance_calls"].append(tool_name)
            if tool_name == "assign_subtask":
                return SubtaskAssignment(
                    id="subtask-builder-001",
                    subtask_id="subtask-builder-001",
                    status="assigned",
                    agent_id="builder",
                    subtask="Do the builder work",
                    deadline_step=1,
                    assigned_by="leader",
                    why_assigned="Test",
                    done_criteria=["Done"],
                    blocking_if_missing=False,
                    provenance="test",
                )
            if tool_name == "report_subtask":
                captured["report_called"] = True
                return SubtaskReport(
                    id="subtask-builder-001",
                    subtask_id="subtask-builder-001",
                    status="completed",
                    agent_id="builder",
                    result=work_result,
                    result_summary="Done",
                    blockers=[],
                    evidence_refs=[],
                    outcome_status="completed",
                    outcome_summary="Done",
                    provenance="agent_report",
                )
            raise AssertionError(f"Unexpected tool_name: {tool_name}")

        async def mock_execute(task, team, subtask, agent, agent_id, work_prompt):
            return work_result, agent, agent_id

        orch._run_governance_tool = mock_governance_tool
        orch._execute_subtask_with_retry = mock_execute
        orch._emit_tool_call = lambda *a, **kw: captured["tool_calls"].append(a[1])
        orch._emit = lambda *a, **kw: None
        orch._record_collaboration_action = lambda *a, **kw: None
        return captured

    def test_analysis_only_builder_reaches_report_without_evidence(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "leader"])
        orch.agents = agents
        prompt = "Analyze the system architecture and summarize tradeoffs"
        task, team, state = _setup_task(orch, ["builder", "leader"], prompt)
        captured = self._install_mocks(orch)

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=None):
            self._run(orch._delegate_subtasks(task, team))

        self.assertTrue(captured["report_called"], "report_subtask should have been called")
        subtask = state["subtasks"][0]
        self.assertNotEqual(subtask.get("status"), "blocked")
        self.assertNotEqual(
            subtask.get("provenance"), "missing_builder_execution_evidence",
        )

    def test_efficient_analysis_records_direct_work_without_two_extra_calls(self) -> None:
        orch = _make_orchestrator(_make_settings(EFFICIENT_SOCIETY_ENABLED=True))
        agents = _make_agents(["builder", "leader"])
        orch.agents = agents
        task, team, state = _setup_task(
            orch,
            ["builder", "leader"],
            "Analyze the system architecture and summarize tradeoffs",
        )
        captured = self._install_mocks(orch, work_result="direct verified analysis")

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=None):
            self._run(orch._delegate_subtasks(task, team))

        self.assertEqual(captured["governance_calls"], [])
        self.assertFalse(captured["report_called"])
        self.assertEqual(state["subtasks"][0]["status"], "completed")
        self.assertEqual(
            state["subtasks"][0]["provenance"],
            "direct_agent_work_product",
        )

    def test_generic_implementation_rejects_notes_demo_evidence(self) -> None:
        """OAuth work must not treat notes-demo files as execution proof."""
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "leader"])
        orch.agents = agents
        prompt = "Implement the login feature with OAuth integration"
        task, team, state = _setup_task(orch, ["builder", "leader"], prompt)
        captured = self._install_mocks(orch)

        notes_evidence = {"passed": True, "artifact_dir": "/tmp/notes", "files": ["server.py", "client.html"]}
        with patch("society.orchestrator.load_notes_demo_evidence", return_value=notes_evidence):
            self._run(orch._delegate_subtasks(task, team))

        self.assertTrue(captured["report_called"], "generic work should reach its normal report path")
        subtask = state["subtasks"][0]
        self.assertNotEqual(subtask.get("status"), "blocked")
        self.assertNotIn("execute_notes_demo", captured["tool_calls"])

    def test_collaborative_notes_request_records_notes_demo_evidence(self) -> None:
        """The dedicated benchmark helper remains available to its own deliverable."""
        orch = _make_orchestrator()
        orch.agents = _make_agents(["builder", "leader"])
        task, team, _state = _setup_task(
            orch, ["builder", "leader"], "Implement a collaborative notes application"
        )
        captured = self._install_mocks(orch)
        notes_evidence = {"passed": True, "artifact_dir": "/tmp/notes", "files": ["server.py", "client.html"]}

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=notes_evidence):
            self._run(orch._delegate_subtasks(task, team))

        self.assertTrue(captured["report_called"])
        self.assertIn("execute_notes_demo", captured["tool_calls"])


if __name__ == "__main__":
    unittest.main()
