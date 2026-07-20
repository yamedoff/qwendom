"""Phase 3: Durable subtask retry/reassignment/recovery semantics.

Tests that:
- Transient failures retry with backoff and succeed when the retry succeeds.
- user_decision and deterministic failures are NOT retried.
- Exhausted subtasks are marked blocked with visible exhaustion reason.
- Capability failures trigger reassignment to a nonduplicate team member.
- Idempotency keys prevent duplicate execution.
- Attempt counts survive session-state reuse (restart persistence).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.error_taxonomy import (
    build_attempt_record,
    classify_error,
    compute_backoff,
    is_retryable,
    make_idempotency_key,
)
from society.memory import EventStore
from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.models import SocietyEvent
from society.orchestrator import SocietyOrchestrator


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
    subtask_id: str = "subtask-test-001",
) -> tuple[TaskRun, Team, dict[str, Any]]:
    task = TaskRun(prompt="Test retry task", status="running")
    orch.tasks[task.id] = task
    team = Team(task_id=task.id, member_ids=list(roster), status="active")
    orch.teams[team.id] = team
    task.team_id = team.id
    state = {
        "subtasks": [],
        "subtask_attempts": {},
        "subtask_idempotency_keys": {},
        "metrics": {"tool_calls": 0, "tool_calls_failed": 0, "governance_rounds": 0, "debate_rounds": 0},
        "phase": "delegation",
        "public_room": {"positions": [], "objections": [], "endorsements": [], "mind_changes": [], "published_private_notes": [], "collaboration_actions": []},
    }
    orch.session_states[task.id] = state
    return task, team, state


class ErrorTaxonomyTests(unittest.TestCase):

    def test_classify_transient(self) -> None:
        self.assertEqual(classify_error(TimeoutError("timed out")), "transient")
        self.assertEqual(classify_error("rate limit exceeded"), "transient")
        self.assertEqual(classify_error("503 service unavailable"), "transient")

    def test_classify_user_decision(self) -> None:
        self.assertEqual(classify_error("user decision required"), "user_decision")
        self.assertEqual(classify_error("tied vote"), "user_decision")

    def test_classify_deterministic(self) -> None:
        self.assertEqual(classify_error("acceptance check failed"), "deterministic")

    def test_classify_capability(self) -> None:
        self.assertEqual(classify_error("tool not available"), "capability")

    def test_classify_format(self) -> None:
        self.assertEqual(classify_error("could not parse tool result JSON"), "format")

    def test_is_retryable(self) -> None:
        self.assertTrue(is_retryable("transient"))
        self.assertTrue(is_retryable("format"))
        self.assertTrue(is_retryable("semantic"))
        self.assertFalse(is_retryable("user_decision"))
        self.assertFalse(is_retryable("deterministic"))


class RestartAttemptReconstructionTests(unittest.TestCase):
    def test_started_attempt_consumes_budget_after_process_restart(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        task_id = "restart-task"
        state = {"subtask_attempts": {}, "subtask_idempotency_keys": {}}
        orch._state = lambda _task_id: state
        orch.events = SimpleNamespace(list=lambda _task_id: [
            SocietyEvent(
                task_id=task_id, type="subtask_attempt_started", message="started",
                actor="builder", payload={
                    "subtask_id": "subtask-r1", "agent_id": "builder",
                    "attempt_number": 1, "max_attempts": 3,
                    "idempotency_key": "idem-1",
                },
            )
        ])

        orch._restore_subtask_attempt_state(task_id, "subtask-r1", 3)

        restored = state["subtask_attempts"]["subtask-r1"]
        self.assertEqual(restored["attempt_count"], 1)
        self.assertEqual(restored["attempts"][0]["next_action"], "interrupted_after_restart")
        self.assertEqual(restored["status"], "blocked")
        self.assertEqual(restored["exhaustion_reason"], "interrupted_requires_reconciliation")
        self.assertEqual(state["subtask_idempotency_keys"]["idem-1"], "interrupted")

    def test_completed_attempt_is_restored_as_terminal(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        task_id = "restart-complete"
        state = {"subtask_attempts": {}, "subtask_idempotency_keys": {}}
        orch._state = lambda _task_id: state
        common = {
            "subtask_id": "subtask-r2", "agent_id": "builder",
            "attempt_number": 1, "max_attempts": 3, "idempotency_key": "idem-2",
        }
        orch.events = SimpleNamespace(list=lambda _task_id: [
            SocietyEvent(task_id=task_id, type="subtask_attempt_started", message="started", actor="builder", payload=common),
            SocietyEvent(task_id=task_id, type="subtask_attempt_completed", message="done", actor="builder", payload=common),
        ])

        orch._restore_subtask_attempt_state(task_id, "subtask-r2", 3)

        self.assertEqual(state["subtask_attempts"]["subtask-r2"]["status"], "completed")
        self.assertEqual(state["subtask_idempotency_keys"]["idem-2"], "completed")

    def test_backoff_is_capped(self) -> None:
        result = compute_backoff(100, base=1.0, cap=30.0, jitter=False)
        self.assertLessEqual(result, 30.0)

    def test_idempotency_key_deterministic(self) -> None:
        k1 = make_idempotency_key("t1", "s1", "a1", 1)
        k2 = make_idempotency_key("t1", "s1", "a1", 1)
        self.assertEqual(k1, k2)
        k3 = make_idempotency_key("t1", "s1", "a1", 2)
        self.assertNotEqual(k1, k3)


class SubtaskRetryTests(unittest.TestCase):

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_transient_success_after_retries(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder", "researcher"])

        subtask = {
            "id": "subtask-001",
            "agent_id": "builder",
            "subtask": "Build the thing",
            "status": "assigned",
        }
        agent = agents["builder"]

        call_count = 0

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("timed out")
            return "success result"

        orch._ask_agent = mock_ask_agent

        result, eff_agent, eff_id = self._run(
            orch._execute_subtask_with_retry(task, team, subtask, agent, "builder", "Build the thing")
        )

        self.assertEqual(result, "success result")
        self.assertEqual(call_count, 3)
        self.assertEqual(eff_id, "builder")

        attempt_state = state["subtask_attempts"]["subtask-001"]
        self.assertEqual(attempt_state["attempt_count"], 3)
        self.assertEqual(attempt_state["status"], "completed")

        event_types = [e.type for e in orch.events.list(task.id)]
        self.assertIn("subtask_attempt_started", event_types)
        self.assertIn("subtask_attempt_failed", event_types)
        self.assertIn("subtask_retry_scheduled", event_types)

    def test_no_retry_for_user_decision(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder"])

        subtask = {
            "id": "subtask-002",
            "agent_id": "builder",
            "subtask": "Decide the thing",
            "status": "assigned",
        }
        agent = agents["builder"]

        call_count = 0

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("user decision required: ambiguous intent")

        orch._ask_agent = mock_ask_agent

        result, _, _ = self._run(
            orch._execute_subtask_with_retry(task, team, subtask, agent, "builder", "Decide the thing")
        )

        self.assertIsNone(result)
        self.assertEqual(call_count, 1)

        attempt_state = state["subtask_attempts"]["subtask-002"]
        self.assertEqual(attempt_state["attempt_count"], 1)
        self.assertEqual(attempt_state["status"], "blocked")
        self.assertIn("non_retryable", attempt_state["exhaustion_reason"])

        event_types = [e.type for e in orch.events.list(task.id)]
        self.assertIn("subtask_exhausted", event_types)
        self.assertNotIn("subtask_retry_scheduled", event_types)

    def test_exhaustion_after_max_attempts(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder"])

        subtask = {
            "id": "subtask-003",
            "agent_id": "builder",
            "subtask": "Build the thing",
            "status": "assigned",
        }
        agent = agents["builder"]

        call_count = 0

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            raise TimeoutError("timed out")

        orch._ask_agent = mock_ask_agent

        result, _, _ = self._run(
            orch._execute_subtask_with_retry(task, team, subtask, agent, "builder", "Build the thing")
        )

        self.assertIsNone(result)
        self.assertEqual(call_count, 3)

        attempt_state = state["subtask_attempts"]["subtask-003"]
        self.assertEqual(attempt_state["attempt_count"], 3)
        self.assertEqual(attempt_state["status"], "blocked")
        self.assertIn("budget_exhausted", attempt_state["exhaustion_reason"])

        event_types = [e.type for e in orch.events.list(task.id)]
        self.assertIn("subtask_exhausted", event_types)
        self.assertEqual(subtask["status"], "blocked")

    def test_reassignment_on_capability_failure(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder", "researcher", "critic"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder", "researcher", "critic"])

        subtask = {
            "id": "subtask-004",
            "agent_id": "builder",
            "subtask": "Build the thing",
            "status": "assigned",
            "required_capabilities": ["evidence"],
        }
        agent = agents["builder"]

        call_count = 0

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            if ag.id == "builder":
                raise RuntimeError("tool not available for this agent")
            return f"success from {ag.id}"

        orch._ask_agent = mock_ask_agent

        result, eff_agent, eff_id = self._run(
            orch._execute_subtask_with_retry(task, team, subtask, agent, "builder", "Build the thing")
        )

        self.assertEqual(result, "success from researcher")
        self.assertEqual(eff_id, "researcher")
        self.assertEqual(subtask["agent_id"], "researcher")

        event_types = [e.type for e in orch.events.list(task.id)]
        self.assertIn("subtask_reassigned", event_types)

    def test_idempotency_prevents_duplicate_execution(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder"])

        subtask = {
            "id": "subtask-005",
            "agent_id": "builder",
            "subtask": "Build the thing",
            "status": "assigned",
        }
        agent = agents["builder"]

        for attempt_num in range(1, 4):
            idem_key = make_idempotency_key(task.id, "subtask-005", "builder", attempt_num)
            state["subtask_idempotency_keys"][idem_key] = "completed"

        call_count = 0

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            return "should not reach"

        orch._ask_agent = mock_ask_agent

        state["subtask_attempts"]["subtask-005"] = {
            "attempt_count": 0,
            "max_attempts": 3,
            "attempts": [],
            "status": "pending",
            "agent_id": "builder",
            "exhaustion_reason": None,
        }

        result, _, _ = self._run(
            orch._execute_subtask_with_retry(task, team, subtask, agent, "builder", "Build the thing")
        )

        self.assertIsNone(result)
        self.assertEqual(call_count, 0)

    def test_completed_attempt_state_is_not_executed_again(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder"])
        subtask = {"id": "subtask-complete", "agent_id": "builder", "status": "completed"}
        state["subtask_attempts"]["subtask-complete"] = {
            "attempt_count": 1, "max_attempts": 3, "attempts": [],
            "status": "completed", "agent_id": "builder", "exhaustion_reason": None,
        }

        result, effective_agent, effective_id = self._run(
            orch._execute_subtask_with_retry(
                task, team, subtask, agents["builder"], "builder", "already done"
            )
        )

        self.assertIsNone(result)
        self.assertEqual(effective_agent.id, "builder")
        self.assertEqual(effective_id, "builder")

    def test_attempt_captures_model_usage_delta(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder"])
        subtask = {"id": "subtask-usage", "agent_id": "builder", "status": "assigned"}

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            state.setdefault("model_usage", []).append({
                "input_tokens": 40, "output_tokens": 10, "total_tokens": 50,
            })
            return "done"

        orch._ask_agent = mock_ask_agent
        self._run(orch._execute_subtask_with_retry(
            task, team, subtask, agents["builder"], "builder", "work"
        ))

        attempt = state["subtask_attempts"]["subtask-usage"]["attempts"][0]
        self.assertEqual(attempt["input_tokens"], 40)
        self.assertEqual(attempt["output_tokens"], 10)
        self.assertEqual(attempt["total_tokens"], 50)

    def test_preserved_attempt_count_across_calls(self) -> None:
        orch = _make_orchestrator()
        agents = _make_agents(["builder"])
        orch.agents = agents
        task, team, state = _setup_task(orch, ["builder"])

        subtask = {
            "id": "subtask-006",
            "agent_id": "builder",
            "subtask": "Build the thing",
            "status": "assigned",
        }
        agent = agents["builder"]

        prev_attempt = build_attempt_record(
            subtask_id="subtask-006",
            agent_id="builder",
            attempt_number=1,
            max_attempts=3,
            idempotency_key=make_idempotency_key(task.id, "subtask-006", "builder", 1),
            category="transient",
        )
        prev_attempt["finished_at"] = 1.0
        prev_attempt["duration_seconds"] = 1.0
        prev_attempt["error_message"] = "TimeoutError: timed out"
        prev_attempt["next_action"] = "retry"

        state["subtask_attempts"]["subtask-006"] = {
            "attempt_count": 1,
            "max_attempts": 3,
            "attempts": [prev_attempt],
            "status": "pending",
            "agent_id": "builder",
            "exhaustion_reason": None,
        }
        idem_key_1 = make_idempotency_key(task.id, "subtask-006", "builder", 1)
        state["subtask_idempotency_keys"][idem_key_1] = "retry_scheduled"

        call_count = 0

        async def mock_ask_agent(ag: SocietyAgent, prompt: str, task_id: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            return "success on resumed attempt"

        orch._ask_agent = mock_ask_agent

        result, _, _ = self._run(
            orch._execute_subtask_with_retry(task, team, subtask, agent, "builder", "Build the thing")
        )

        self.assertEqual(result, "success on resumed attempt")
        self.assertEqual(call_count, 1)

        attempt_state = state["subtask_attempts"]["subtask-006"]
        self.assertEqual(attempt_state["attempt_count"], 2)
        self.assertEqual(len(attempt_state["attempts"]), 2)
        self.assertEqual(attempt_state["attempts"][0]["attempt_number"], 1)
        self.assertEqual(attempt_state["attempts"][1]["attempt_number"], 2)
        self.assertEqual(attempt_state["status"], "completed")


if __name__ == "__main__":
    unittest.main()
