from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator, GovernanceToolError
from society.schemas.governance import VoteDecision
from config import Settings


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "qwen",
        "QWEN_API_KEY": "test-key",
        "QWEN_MODEL": "qwen3.7-plus",
        "LLM_TIMEOUT_SECONDS": 10,
        "READINESS_CONCURRENCY": 3,
        "EFFICIENT_SOCIETY_ENABLED": False,
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
        "ready_to_proceed": True,
        "ballots": [],
        "proposals": {aid: {"proposal": f"prop-{aid}", "proposal_id": f"prop-id-{aid}"} for aid in roster},
        "metrics": {"tool_calls": 0, "tool_calls_failed": 0, "governance_rounds": 0, "debate_rounds": 0},
        "phase": "voting",
        "public_room": {"positions": [], "objections": [], "endorsements": [], "mind_changes": [], "published_private_notes": [], "collaboration_actions": []},
        "private_agent_state": {},
        "trust_updates": [],
    }
    return task, team


class NativeBallotPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_voting_persists_ballots_to_live_state(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        vote_map = {
            "architect": "builder",
            "researcher": "builder",
            "builder": "architect",
            "critic": "builder",
        }

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(
                choice=vote_map[actor_identity.id],
                reason=f"{actor_identity.id} voted",
                confidence=0.8,
            )

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            from collections import Counter
            ballots = state.get("ballots", [])
            votes = Counter(b["choice"] for b in ballots)
            tally = dict(votes.most_common())
            winner = votes.most_common(1)[0][0] if votes else None
            state["tally"] = tally
            state["winner_id"] = winner
            state["phase"] = "voted"
            return TallyResult(tally=tally, winner=winner)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                with patch.object(SocietyOrchestrator, "_emit_leader_synthesis", new=AsyncMock()):
                    winner = await orch._vote(task, team, dict(zip(roster, [f"prop-{r}" for r in roster])))

        ballots = orch._state(task.id)["ballots"]
        self.assertEqual(len(ballots), len(roster))
        voter_ids = {b["voter"] for b in ballots}
        self.assertEqual(voter_ids, set(roster))
        for ballot in ballots:
            self.assertEqual(ballot["choice"], vote_map[ballot["voter"]])
        self.assertEqual(orch._state(task.id)["winner_id"], "builder")
        self.assertEqual(winner, "builder")

    async def test_native_voting_no_duplicate_ballots(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            from collections import Counter
            ballots = state.get("ballots", [])
            votes = Counter(b["choice"] for b in ballots)
            tally = dict(votes.most_common())
            winner = votes.most_common(1)[0][0] if votes else None
            state["tally"] = tally
            state["winner_id"] = winner
            state["phase"] = "voted"
            return TallyResult(tally=tally, winner=winner)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                with patch.object(SocietyOrchestrator, "_emit_leader_synthesis", new=AsyncMock()):
                    await orch._vote(task, team, dict(zip(roster, [f"prop-{r}" for r in roster])))

        ballots = orch._state(task.id)["ballots"]
        voter_ids = [b["voter"] for b in ballots]
        self.assertEqual(len(voter_ids), len(set(voter_ids)))


class TallyValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_tally_pauses_for_user(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["ballots"] = []
            state["tally"] = {}
            state["winner_id"] = None
            state["phase"] = "voted"
            return TallyResult(tally={}, winner=None)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                result = await orch._vote(task, team, dict(zip(roster, [f"prop-{r}" for r in roster])))

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")
        self.assertEqual(orch._state(task.id).get("resume_phase"), "vote_resolution")

    async def test_invalid_winner_pauses_for_user(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"nonexistent_agent": 4}
            state["winner_id"] = "nonexistent_agent"
            state["phase"] = "voted"
            return TallyResult(tally={"nonexistent_agent": 4}, winner="nonexistent_agent")

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                result = await orch._vote(task, team, dict(zip(roster, [f"prop-{r}" for r in roster])))

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")

    async def test_empty_string_winner_pauses_for_user(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {}
            state["winner_id"] = ""
            state["phase"] = "voted"
            return TallyResult(tally={}, winner="")

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                result = await orch._vote(task, team, dict(zip(roster, [f"prop-{r}" for r in roster])))

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")


class PostReadinessExceptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_exception_sets_failed_and_emits_events(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"
        orch._state(task.id)["phase"] = "leader_elected"

        async def failing_run_agno_team(task, team):
            return None

        async def raising_method(task, team):
            raise RuntimeError("Unexpected post-readiness failure in _elect_leader")

        orch._run_agno_team = failing_run_agno_team
        orch._elect_leader = raising_method

        await orch._execute_after_readiness(task, team, 0.0)

        self.assertEqual(task.status, "failed")
        event_types = [getattr(e, "type", None) for e in orch.events._events]
        self.assertIn("run_failed", event_types)
        self.assertIn("task_failed", event_types)

    async def test_generic_exception_preserves_error_message(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"
        orch._state(task.id)["phase"] = "negotiating"

        async def noop(task, team):
            return None

        async def raising_negotiate(task, team):
            raise ValueError("Specific negotiation error detail")

        async def skip_team_composition(task, team):
            # This test exercises the generic post-readiness exception handler,
            # not the separately tested composition runtime.  Isolate the
            # intervening lifecycle phase so no provider-backed specialist
            # selection can preempt the injected negotiation failure.
            return "disabled"

        orch._run_agno_team = noop
        orch._elect_leader = noop
        orch._spawn_child_agent = noop
        orch._run_team_composition_phase = skip_team_composition
        orch._negotiate = raising_negotiate

        await orch._execute_after_readiness(task, team, 0.0)

        self.assertEqual(task.status, "failed")
        failed_events = [e for e in orch.events._events if getattr(e, "type", None) == "task_failed"]
        self.assertTrue(len(failed_events) > 0)
        last_failed = failed_events[-1]
        self.assertIn("Specific negotiation error detail", getattr(last_failed, "message", "") or str(last_failed.payload))

    async def test_governance_tool_error_still_handled(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder", "critic"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"
        orch._state(task.id)["phase"] = "leader_elected"

        async def noop(task, team):
            return None

        async def raising_method(task, team):
            raise GovernanceToolError("Governance tool broke")

        orch._run_agno_team = noop
        orch._elect_leader = raising_method

        await orch._execute_after_readiness(task, team, 0.0)

        self.assertEqual(task.status, "failed")
        event_types = [getattr(e, "type", None) for e in orch.events._events]
        self.assertIn("run_failed", event_types)
        self.assertIn("task_failed", event_types)
        task_failed_events = [e for e in orch.events._events if getattr(e, "type", None) == "task_failed"]
        gov_failure = [e for e in task_failed_events if "Governance tool failure" in (getattr(e, "message", "") or "")]
        self.assertTrue(len(gov_failure) > 0)


if __name__ == "__main__":
    unittest.main()
