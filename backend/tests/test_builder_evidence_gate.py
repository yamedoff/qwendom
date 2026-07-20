"""Regression tests for the builder execution-evidence gate.

The gate in _delegate_subtasks must demand execute_notes_demo evidence
ONLY when the overall brief genuinely requires implementation.
Analysis-only briefs must let builder contributions pass through to
the report phase without execution evidence.

Two paths are covered:
  1. Implementation brief + no evidence  -> subtask blocked  (hard gate retained)
  2. Analysis-only brief  + no evidence  -> subtask proceeds to report
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
        "LLM_PROVIDER": "qwen",
        "QWEN_API_KEY": "test-key",
        "QWEN_MODEL": "qwen3.7-plus",
        "LLM_TIMEOUT_SECONDS": 10,
        "READINESS_CONCURRENCY": 3,
        "SUBTASK_MAX_ATTEMPTS": 3,
        "SUBTASK_BACKOFF_BASE_SECONDS": 0.001,
        "SUBTASK_BACKOFF_CAP_SECONDS": 0.01,
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


def _setup(
    orch: SocietyOrchestrator,
    prompt: str,
    roster: list[str],
) -> tuple[TaskRun, Team, dict[str, Any]]:
    task = TaskRun(prompt=prompt, status="running")
    orch.tasks[task.id] = task
    team = Team(task_id=task.id, member_ids=list(roster), status="active")
    orch.teams[team.id] = team
    task.team_id = team.id
    builder_subtask = {
        "id": "subtask-builder-001",
        "agent_id": "builder",
        "subtask": "Builder work item",
        "status": "planned",
        "done_criteria": ["done"],
        "why_assigned": "test",
        "blocking_if_missing": False,
    }
    state: dict[str, Any] = {
        "subtasks": [builder_subtask],
        "subtask_attempts": {},
        "subtask_idempotency_keys": {},
        "metrics": {"tool_calls": 0, "tool_calls_failed": 0, "governance_rounds": 0, "debate_rounds": 0},
        "phase": "delegation",
        "public_room": {"positions": [], "objections": [], "endorsements": [], "mind_changes": [], "published_private_notes": [], "collaboration_actions": []},
    }
    orch.session_states[task.id] = state
    return task, team, state


class BuilderEvidenceGateTests(unittest.TestCase):

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def _wire_orch(
        self,
        orch: SocietyOrchestrator,
        task: TaskRun,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        emitted: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []

        async def fake_governance_tool(*, task: Any, actor_identity: Any, tool_func: Any, tool_name: str, schema_class: Any, prompt: str) -> Any:
            if tool_name == "assign_subtask":
                return SubtaskAssignment(
                    id="subtask-builder-001",
                    subtask_id="subtask-builder-001",
                    status="assigned",
                    agent_id="builder",
                    subtask="Builder work item",
                    why_assigned="test",
                    done_criteria=["done"],
                    blocking_if_missing=False,
                    provenance="test",
                )
            if tool_name == "report_subtask":
                return SubtaskReport(
                    id="subtask-builder-001",
                    subtask_id="subtask-builder-001",
                    status="completed",
                    agent_id="builder",
                    result="analysis complete",
                    result_summary="analysis complete",
                    blockers=[],
                    evidence_refs=["notes"],
                    outcome_status="completed",
                    outcome_summary="analysis complete",
                    provenance="agent_report",
                )
            raise AssertionError(f"Unexpected tool: {tool_name}")

        async def fake_execute(*args: Any, **kwargs: Any) -> tuple[str, SocietyAgent, str]:
            return "analysis complete", orch.agents["builder"], "builder"

        orch._run_governance_tool = fake_governance_tool
        orch._execute_subtask_with_retry = fake_execute
        orch._emit_tool_call = lambda *a, **kw: tool_calls.append({"args": a, "kwargs": kw})
        orch._emit = lambda _tid, etype, msg, **kw: emitted.append({"type": etype, "message": msg, "payload": kw.get("payload", {})})
        return emitted, tool_calls

    def test_generic_implementation_does_not_require_notes_demo_evidence(self) -> None:
        """Generic code work cannot be routed through notes-demo evidence."""
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher"])
        orch.agents = agents
        task, team, state = _setup(orch, "Implement a REST API for user management", ["builder", "researcher"])
        emitted, tool_calls = self._wire_orch(orch, task)

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=None):
            self._run(orch._delegate_subtasks(task, team))

        builder_subtask = state["subtasks"][0]
        self.assertNotEqual(builder_subtask["status"], "blocked")
        self.assertFalse(any(tc["args"] and tc["args"][1] == "execute_notes_demo" for tc in tool_calls))

    def test_analysis_brief_lets_builder_proceed_without_evidence(self) -> None:
        """Analysis-only briefs must NOT block builder for missing execute_notes_demo."""
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher"])
        orch.agents = agents
        task, team, state = _setup(orch, "Analyze the risks of the proposed architecture", ["builder", "researcher"])
        emitted, tool_calls = self._wire_orch(orch, task)

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=None):
            self._run(orch._delegate_subtasks(task, team))

        builder_subtask = state["subtasks"][0]
        self.assertNotEqual(builder_subtask.get("provenance"), "missing_builder_execution_evidence")
        self.assertNotEqual(builder_subtask.get("status"), "blocked",
                            "Analysis-only brief should not block builder for missing execution evidence")
        report_events = [e for e in emitted if e["type"] == "delegation_reported"]
        self.assertTrue(len(report_events) >= 1, "Builder should reach the report phase for analysis briefs")
        report_payload = report_events[-1]["payload"]
        self.assertNotEqual(report_payload.get("status"), "blocked")

    def test_benchmark_audit_does_not_trigger_product_execution_gate(self) -> None:
        """Benchmark fixture keywords must not demand unrelated notes-demo evidence."""

        settings = _make_settings(BENCHMARK_SUITE_TOOLS_ENABLED=True)
        orch = _make_orchestrator(settings)
        orch.agents = _make_agents(["builder", "researcher"])
        task, team, state = _setup(
            orch,
            "Audit release implementation and deployment security blockers",
            ["builder", "researcher"],
        )
        emitted, _ = self._wire_orch(orch, task)

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=None):
            self._run(orch._delegate_subtasks(task, team))

        builder_subtask = state["subtasks"][0]
        self.assertNotEqual(builder_subtask.get("status"), "blocked")
        self.assertNotEqual(
            builder_subtask.get("provenance"), "missing_builder_execution_evidence"
        )
        self.assertFalse(any(event["type"] == "builder_subtask_required" for event in emitted))

    def test_decision_memo_brief_lets_builder_proceed_without_evidence(self) -> None:
        """Decision memo briefs with explicit no-implementation must NOT block builder."""
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher"])
        orch.agents = agents
        # This is the exact failed judge brief from the issue
        prompt = "Produce a decision memo; analysis only; no implementation required"
        task, team, state = _setup(orch, prompt, ["builder", "researcher"])
        emitted, tool_calls = self._wire_orch(orch, task)

        with patch("society.orchestrator.load_notes_demo_evidence", return_value=None):
            self._run(orch._delegate_subtasks(task, team))

        builder_subtask = state["subtasks"][0]
        self.assertNotEqual(builder_subtask.get("provenance"), "missing_builder_execution_evidence",
                            "Decision memo brief should not trigger execution evidence gate")
        self.assertNotEqual(builder_subtask.get("status"), "blocked",
                            "Decision memo brief should not block builder for missing execution evidence")

    def test_implementation_brief_with_evidence_proceeds(self) -> None:
        """Implementation briefs with valid evidence must proceed normally."""
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher"])
        orch.agents = agents
        task, team, state = _setup(orch, "Implement a collaborative notes application", ["builder", "researcher"])
        emitted, tool_calls = self._wire_orch(orch, task)

        fake_evidence = {"passed": True, "artifact_dir": "/tmp/art", "files": ["main.py"]}
        with patch("society.orchestrator.load_notes_demo_evidence", return_value=fake_evidence):
            self._run(orch._delegate_subtasks(task, team))

        builder_subtask = state["subtasks"][0]
        self.assertNotEqual(builder_subtask.get("status"), "blocked")
        self.assertNotEqual(builder_subtask.get("provenance"), "missing_builder_execution_evidence")

    def test_analysis_brief_does_not_record_notes_demo_evidence(self) -> None:
        """Unrelated briefs must not emit benchmark demo evidence."""
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher"])
        orch.agents = agents
        task, team, state = _setup(orch, "Analyze the risks of the proposed architecture", ["builder", "researcher"])
        emitted, tool_calls = self._wire_orch(orch, task)

        fake_evidence = {"passed": True, "artifact_dir": "/tmp/art", "files": ["notes.md"]}
        with patch("society.orchestrator.load_notes_demo_evidence", return_value=fake_evidence):
            self._run(orch._delegate_subtasks(task, team))

        builder_subtask = state["subtasks"][0]
        self.assertNotEqual(builder_subtask.get("status"), "blocked")
        demo_tool_calls = [tc for tc in tool_calls if tc["args"] and tc["args"][1] == "execute_notes_demo"]
        self.assertEqual(demo_tool_calls, [])


if __name__ == "__main__":
    unittest.main()
