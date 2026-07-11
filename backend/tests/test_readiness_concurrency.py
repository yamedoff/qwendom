from __future__ import annotations

import asyncio
import copy
import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.conversation import ReadinessBallot
from society.schemas.governance import VoteDecision
from config import Settings


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "cerebras",
        "CEREBRAS_API_KEY": "test-key",
        "CEREBRAS_MODEL": "test-model",
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


class ReadinessConcurrencyOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_ballots_returned_in_roster_order(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        completion_order: list[str] = []
        delays = {
            "architect": 0.06,
            "researcher": 0.01,
            "builder": 0.04,
            "critic": 0.02,
        }

        async def fake_isolated(self_orch, *, actor_identity, derived_session_id, state_snapshot, **kwargs):
            agent_id = actor_identity.id
            completion_order.append(agent_id)
            await asyncio.sleep(delays[agent_id])
            return ReadinessBallot(
                attempt=1,
                agent_id=agent_id,
                ready=True,
                critical_blocker=False,
                reason=f"{agent_id} ready",
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            results = await orch._collect_readiness_ballots_concurrent(task, team, attempt=1)

        result_ids = [agent_id for agent_id, _ in results]
        self.assertEqual(result_ids, roster)
        self.assertNotEqual(completion_order, roster)

    async def test_readiness_events_emitted_in_roster_order(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            delay = {"architect": 0.05, "researcher": 0.01, "builder": 0.03, "critic": 0.02}[actor_identity.id]
            await asyncio.sleep(delay)
            return ReadinessBallot(
                attempt=1,
                agent_id=actor_identity.id,
                ready=True,
                critical_blocker=False,
                reason=f"{actor_identity.id} ready",
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            passed = await orch._run_readiness_vote(task, team, attempt=1)

        self.assertTrue(passed)
        vote_events = [e for e in orch.events._events if getattr(e, "type", None) == "readiness_vote_cast"]
        event_actor_order = [getattr(e, "actor", None) for e in vote_events]
        self.assertEqual(event_actor_order, roster)


class VoteConcurrencyOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_votes_returned_in_roster_order(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        candidates = list(roster)

        completion_order: list[str] = []
        delays = {
            "architect": 0.05,
            "researcher": 0.01,
            "builder": 0.04,
            "critic": 0.02,
        }

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            agent_id = actor_identity.id
            completion_order.append(agent_id)
            await asyncio.sleep(delays[agent_id])
            return VoteDecision(
                choice="architect",
                reason=f"{agent_id} voted",
                confidence=0.8,
            )

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            results = await orch._collect_votes_concurrent(task, team, candidates)

        result_ids = [voter_id for voter_id, _ in results]
        self.assertEqual(result_ids, roster)
        self.assertNotEqual(completion_order, roster)


class DistinctSessionIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_each_call_gets_unique_session_id(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        seen_session_ids: list[str] = []

        async def fake_isolated(self_orch, *, derived_session_id, **kwargs):
            seen_session_ids.append(derived_session_id)
            return ReadinessBallot(attempt=1, agent_id="x", ready=True)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._collect_readiness_ballots_concurrent(task, team, attempt=1)

        self.assertEqual(len(seen_session_ids), len(roster))
        self.assertEqual(len(set(seen_session_ids)), len(seen_session_ids))
        for sid in seen_session_ids:
            self.assertIn(":readiness:", sid)
            self.assertIn(task.id, sid)

    async def test_vote_each_call_gets_unique_session_id(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        seen_session_ids: list[str] = []

        async def fake_isolated(self_orch, *, derived_session_id, **kwargs):
            seen_session_ids.append(derived_session_id)
            return VoteDecision(choice="architect", reason="r", confidence=0.5)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._collect_votes_concurrent(task, team, list(roster))

        self.assertEqual(len(seen_session_ids), len(roster))
        self.assertEqual(len(set(seen_session_ids)), len(seen_session_ids))
        for sid in seen_session_ids:
            self.assertIn(":vote:", sid)
            self.assertIn(task.id, sid)


class SnapshotIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_snapshots_are_independent_deep_copies(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        orch._state(task.id)["shared_marker"] = "original"

        captured_snapshots: list[dict] = []

        async def fake_isolated(self_orch, *, state_snapshot, **kwargs):
            state_snapshot["shared_marker"] = "mutated"
            state_snapshot.setdefault("snapshot_unique_key", []).append("added")
            captured_snapshots.append(state_snapshot)
            return ReadinessBallot(attempt=1, agent_id="x", ready=True)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._collect_readiness_ballots_concurrent(task, team, attempt=1)

        self.assertEqual(len(captured_snapshots), len(roster))
        live_state = orch._state(task.id)
        self.assertEqual(live_state["shared_marker"], "original")
        self.assertNotIn("snapshot_unique_key", live_state)
        for snap in captured_snapshots:
            self.assertIsNot(snap, live_state)
            self.assertEqual(snap["shared_marker"], "mutated")
        keys = [id(snap) for snap in captured_snapshots]
        self.assertEqual(len(set(keys)), len(keys))

    async def test_vote_snapshots_do_not_mutate_live_state(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        orch._state(task.id)["vote_marker"] = "untouched"

        async def fake_isolated(self_orch, *, state_snapshot, **kwargs):
            state_snapshot["vote_marker"] = "changed"
            return VoteDecision(choice="architect", reason="r", confidence=0.5)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._collect_votes_concurrent(task, team, list(roster))

        self.assertEqual(orch._state(task.id)["vote_marker"], "untouched")


class BoundedConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_semaphore_limits_concurrent_calls(self) -> None:
        orch = _make_orchestrator(_make_settings(READINESS_CONCURRENCY=2))
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        peak_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def fake_isolated(self_orch, **kwargs):
            nonlocal peak_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > peak_concurrent:
                    peak_concurrent = current_concurrent
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1
            return ReadinessBallot(attempt=1, agent_id="x", ready=True)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._collect_readiness_ballots_concurrent(task, team, attempt=1)

        self.assertLessEqual(peak_concurrent, 2)
        self.assertGreater(peak_concurrent, 0)


class NoConcurrentStateMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_does_not_write_current_actor_during_collect(self) -> None:
        orch = _make_orchestrator()
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        orch._state(task.id)["current_actor"] = "nobody"
        actors_seen_during_collect: list[str] = []

        original_state = orch._state(task.id)

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            actors_seen_during_collect.append(actor_identity.id)
            await asyncio.sleep(0.01)
            return ReadinessBallot(attempt=1, agent_id=actor_identity.id, ready=True)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            await orch._collect_readiness_ballots_concurrent(task, team, attempt=1)

        self.assertEqual(orch._state(task.id)["current_actor"], "nobody")
        self.assertIs(orch._state(task.id), original_state)


if __name__ == "__main__":
    unittest.main()
