"""Phase 6 typed readiness blocker tests.

Proves:
1. future_work does not block execution.
2. missing_user_input does block execution.
3. Structured blocker payload survives the readiness tally.
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator, _is_structured_execution_blocker
from society.schemas.conversation import (
    BLOCKING_CATEGORIES,
    ReadinessBallot,
    ReadinessBlocker,
)
from config import Settings


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "qwen",
        "QWEN_API_KEY": "test-key",
        "QWEN_MODEL": "qwen3.7-plus",
        "LLM_TIMEOUT_SECONDS": 10,
        "READINESS_CONCURRENCY": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_orchestrator(settings: Settings | None = None) -> SocietyOrchestrator:
    settings = settings or _make_settings()
    with patch("society.orchestrator.get_agno_db", return_value=None):
        with patch("society.orchestrator.get_settings", return_value=settings):
            orch = SocietyOrchestrator(settings=settings)
    orch.events = _FakeEventStore()
    return orch


class _FakeEventStore:
    def __init__(self) -> None:
        self._events: list = []

    def append(self, event: object) -> None:
        self._events.append(event)

    def list(self, task_id: str | None = None) -> list:
        if task_id is None:
            return list(self._events)
        return [e for e in self._events if getattr(e, "task_id", None) == task_id]

    def task_summaries(self) -> list:
        return []

    def task_summary(self, task_id: str) -> dict | None:
        return None

    def agent_memory(self, agent_id: str) -> list:
        return []


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


def _make_task_and_team(orch: SocietyOrchestrator, roster: list[str]) -> tuple[TaskRun, Team]:
    task = TaskRun(prompt="Test task", status="running")
    orch.tasks[task.id] = task
    team = Team(task_id=task.id, member_ids=list(roster), status="active")
    orch.teams[team.id] = team
    task.team_id = team.id
    orch.session_states[task.id] = {
        "goal_discussions": [],
        "discussion_round_count": 1,
        "readiness_tally": {},
        "ready_to_proceed": False,
        "ballots": [],
        "proposals": {aid: {"proposal": f"prop-{aid}"} for aid in roster},
        "metrics": {"tool_calls": 0, "tool_calls_failed": 0, "governance_rounds": 0, "debate_rounds": 0},
        "phase": "voting",
        "public_room": {"positions": [], "objections": [], "endorsements": [], "mind_changes": [], "published_private_notes": [], "collaboration_actions": []},
        "private_agent_state": {},
        "trust_updates": [],
    }
    return task, team


class StructuredBlockerDecisionTests(unittest.TestCase):
    """_is_structured_execution_blocker decides from typed categories, not keywords."""

    def test_future_work_does_not_block(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="critic",
            ready=False,
            critical_blocker=True,
            reason="Tests have not yet been run",
            blocker_category="future_work",
            owner="builder",
            phase="execution",
            remediation="Run tests during the work phase",
        )
        self.assertFalse(_is_structured_execution_blocker(ballot))

    def test_risk_does_not_block(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="critic",
            ready=False,
            critical_blocker=True,
            reason="Edge cases may fail under load",
            blocker_category="risk",
            owner="critic",
            phase="execution",
            remediation="Monitor during execution",
        )
        self.assertFalse(_is_structured_execution_blocker(ballot))

    def test_missing_user_input_blocks(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="architect",
            ready=False,
            critical_blocker=True,
            reason="User must specify target deployment environment",
            blocker_category="missing_user_input",
            owner="user",
            phase="pre_execution",
            remediation="Ask user which environment to target",
        )
        self.assertTrue(_is_structured_execution_blocker(ballot))

    def test_missing_system_capability_blocks(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="builder",
            ready=False,
            critical_blocker=True,
            reason="Sandbox execution environment is not available",
            blocker_category="missing_system_capability",
            owner="system",
            phase="pre_execution",
            remediation="Provision sandbox before execution",
        )
        self.assertTrue(_is_structured_execution_blocker(ballot))

    def test_safety_or_policy_blocks(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="critic",
            ready=False,
            critical_blocker=True,
            reason="Constraints conflict: ship today vs. run full security audit",
            blocker_category="safety_or_policy",
            owner="user",
            phase="pre_execution",
            remediation="User must resolve the policy conflict",
        )
        self.assertTrue(_is_structured_execution_blocker(ballot))

    def test_blocking_categories_are_correct(self) -> None:
        self.assertIn("missing_user_input", BLOCKING_CATEGORIES)
        self.assertIn("missing_system_capability", BLOCKING_CATEGORIES)
        self.assertIn("safety_or_policy", BLOCKING_CATEGORIES)
        self.assertNotIn("future_work", BLOCKING_CATEGORIES)
        self.assertNotIn("risk", BLOCKING_CATEGORIES)

    def test_non_critical_blocker_never_blocks(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="architect",
            ready=True,
            critical_blocker=False,
            reason="All clear",
            blocker_category="missing_user_input",
        )
        self.assertFalse(_is_structured_execution_blocker(ballot))

    def test_legacy_uncategorized_critical_ballot_blocks_without_text_inference(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="critic",
            ready=False,
            critical_blocker=True,
            reason="Builder has not yet produced implementation artifacts",
        )
        self.assertTrue(_is_structured_execution_blocker(ballot))

    def test_backward_compat_blocks_real_user_input_without_category(self) -> None:
        ballot = ReadinessBallot(
            attempt=1,
            agent_id="architect",
            ready=False,
            critical_blocker=True,
            reason="User must decide the target market scope",
        )
        self.assertTrue(_is_structured_execution_blocker(ballot))


class FutureWorkNonBlockingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A future_work critical_blocker does not prevent readiness from passing."""

    async def test_future_work_ballot_does_not_block_readiness(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            agent_id = actor_identity.id
            if agent_id == "critic":
                return ReadinessBallot(
                    attempt=1,
                    agent_id=agent_id,
                    ready=False,
                    critical_blocker=True,
                    reason="Tests have not yet been run; implementation not yet complete",
                    blocker_category="future_work",
                    owner="builder",
                    phase="execution",
                    remediation="Run tests during work phase",
                )
            return ReadinessBallot(
                attempt=1,
                agent_id=agent_id,
                ready=True,
                critical_blocker=False,
                reason=f"{agent_id} ready",
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            passed = await orch._run_readiness_vote(task, team, attempt=1)

        self.assertTrue(passed)
        tally = orch._state(task.id)["readiness_tally"]
        self.assertEqual(tally["structured_blockers"], [])


class MissingUserInputBlockingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A missing_user_input critical_blocker prevents readiness from passing."""

    async def test_missing_user_input_blocks_readiness(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            agent_id = actor_identity.id
            if agent_id == "architect":
                return ReadinessBallot(
                    attempt=1,
                    agent_id=agent_id,
                    ready=False,
                    critical_blocker=True,
                    reason="User must specify the deployment target",
                    blocker_category="missing_user_input",
                    owner="user",
                    phase="pre_execution",
                    remediation="Ask user which environment to target",
                )
            return ReadinessBallot(
                attempt=1,
                agent_id=agent_id,
                ready=True,
                critical_blocker=False,
                reason=f"{agent_id} ready",
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            passed = await orch._run_readiness_vote(task, team, attempt=1)

        self.assertFalse(passed)
        tally = orch._state(task.id)["readiness_tally"]
        self.assertFalse(tally["passed"])
        self.assertEqual(len(tally["structured_blockers"]), 1)
        self.assertEqual(tally["structured_blockers"][0]["category"], "missing_user_input")


class StructuredPayloadSurvivesTallyTests(unittest.IsolatedAsyncioTestCase):
    """Structured blocker metadata persists through the readiness tally."""

    async def test_structured_blockers_in_tally_payload(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            agent_id = actor_identity.id
            if agent_id == "critic":
                return ReadinessBallot(
                    attempt=1,
                    agent_id=agent_id,
                    ready=False,
                    critical_blocker=True,
                    reason="Conflicting safety constraints require user resolution",
                    blocker_category="safety_or_policy",
                    owner="user",
                    phase="pre_execution",
                    remediation="User must resolve the policy conflict",
                )
            if agent_id == "builder":
                return ReadinessBallot(
                    attempt=1,
                    agent_id=agent_id,
                    ready=False,
                    critical_blocker=True,
                    reason="Sandbox not provisioned",
                    blocker_category="missing_system_capability",
                    owner="system",
                    phase="pre_execution",
                    remediation="Provision sandbox before execution",
                )
            return ReadinessBallot(
                attempt=1,
                agent_id=agent_id,
                ready=True,
                critical_blocker=False,
                reason=f"{agent_id} ready",
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            passed = await orch._run_readiness_vote(task, team, attempt=1)

        self.assertFalse(passed)
        tally = orch._state(task.id)["readiness_tally"]
        structured = tally["structured_blockers"]
        self.assertEqual(len(structured), 2)

        categories = {b["category"] for b in structured}
        self.assertEqual(categories, {"safety_or_policy", "missing_system_capability"})

        for blocker in structured:
            self.assertIn("category", blocker)
            self.assertIn("owner", blocker)
            self.assertIn("phase", blocker)
            self.assertIn("remediation", blocker)
            self.assertIn("reason", blocker)

        safety_blocker = next(b for b in structured if b["category"] == "safety_or_policy")
        self.assertEqual(safety_blocker["owner"], "user")
        self.assertEqual(safety_blocker["phase"], "pre_execution")
        self.assertEqual(safety_blocker["remediation"], "User must resolve the policy conflict")

        capability_blocker = next(b for b in structured if b["category"] == "missing_system_capability")
        self.assertEqual(capability_blocker["owner"], "system")
        self.assertEqual(capability_blocker["remediation"], "Provision sandbox before execution")

    async def test_string_blockers_still_populated_for_compatibility(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            agent_id = actor_identity.id
            if agent_id == "critic":
                return ReadinessBallot(
                    attempt=1,
                    agent_id=agent_id,
                    ready=False,
                    critical_blocker=True,
                    reason="User must choose the target market",
                    blocker_category="missing_user_input",
                    owner="user",
                    phase="pre_execution",
                    remediation="Ask user to pick a market",
                )
            return ReadinessBallot(
                attempt=1,
                agent_id=agent_id,
                ready=True,
                critical_blocker=False,
                reason=f"{agent_id} ready",
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._run_readiness_vote(task, team, attempt=1)

        tally = orch._state(task.id)["readiness_tally"]
        self.assertTrue(len(tally["blockers"]) > 0)
        self.assertIn("critic", tally["blockers"][0])


class ReadinessBlockerModelTests(unittest.TestCase):
    """ReadinessBlocker model validates and serializes correctly."""

    def test_blocker_model_round_trip(self) -> None:
        blocker = ReadinessBlocker(
            category="missing_user_input",
            owner="user",
            phase="pre_execution",
            remediation="Ask user for clarification",
            reason="Ambiguous scope",
        )
        data = blocker.model_dump()
        restored = ReadinessBlocker.model_validate(data)
        self.assertEqual(restored.category, "missing_user_input")
        self.assertEqual(restored.owner, "user")
        self.assertEqual(restored.phase, "pre_execution")
        self.assertEqual(restored.remediation, "Ask user for clarification")
        self.assertEqual(restored.reason, "Ambiguous scope")


if __name__ == "__main__":
    unittest.main()
