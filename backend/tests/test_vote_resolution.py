from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.governance import VoteDecision
from config import Settings


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "qwen_legacy",
        "QWEN_LEGACY_API_KEY": "test-key",
        "QWEN_LEGACY_MODEL": "test-model",
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


class TiePauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tally_tie_with_arbitrary_winner_pauses_for_user(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        proposals = {aid: f"prop-{aid}" for aid in roster}

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            vote_map = {"architect": "architect", "researcher": "researcher"}
            return VoteDecision(choice=vote_map[actor_identity.id], reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"architect": 2, "researcher": 2}
            state["winner_id"] = "architect"
            state["phase"] = "voted"
            return TallyResult(tally={"architect": 2, "researcher": 2}, winner="architect")

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                result = await orch._vote(task, team, proposals)

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")
        state = orch._state(task.id)
        self.assertEqual(state.get("resume_phase"), "vote_resolution")
        self.assertIsNone(state.get("winner_id"))
        tied_events = [e for e in orch.events._events if getattr(e, "type", None) == "ballots_tallied"]
        self.assertTrue(len(tied_events) > 0)
        last_tally_event = tied_events[-1]
        self.assertIn("tied_candidates", last_tally_event.payload)
        self.assertEqual(set(last_tally_event.payload["tied_candidates"]), {"architect", "researcher"})

    async def test_native_tally_normalizes_invalid_keys_and_non_numeric_counts(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        proposals = {aid: f"prop-{aid}" for aid in roster}

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"architect": 2, "researcher": 2, "nonexistent": 5}
            state["winner_id"] = "architect"
            state["phase"] = "voted"
            return TallyResult(tally={"architect": 2, "researcher": 2, "nonexistent": 5}, winner="architect")

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                result = await orch._vote(task, team, proposals)

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")
        state = orch._state(task.id)
        stored_tally = state.get("tally", {})
        self.assertNotIn("nonexistent", stored_tally)
        self.assertEqual(stored_tally.get("architect"), 2)
        self.assertEqual(stored_tally.get("researcher"), 2)

    async def test_explicit_tally_tie_pauses(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            vote_map = {"architect": "builder", "researcher": "architect", "builder": "researcher"}
            return VoteDecision(choice=vote_map[actor_identity.id], reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"builder": 1, "architect": 1, "researcher": 1}
            state["winner_id"] = None
            state["phase"] = "voted"
            return TallyResult(tally={"builder": 1, "architect": 1, "researcher": 1}, winner=None)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                result = await orch._vote(task, team, {aid: f"prop-{aid}" for aid in roster})

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")
        self.assertEqual(orch._state(task.id).get("resume_phase"), "vote_resolution")


class LeaderQuestionPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_leader_question_contains_tally_and_candidates(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        proposals = {aid: f"proposal-text-for-{aid}" for aid in roster}

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"architect": 1, "researcher": 1, "builder": 1}
            state["winner_id"] = None
            state["phase"] = "voted"
            return TallyResult(tally={"architect": 1, "researcher": 1, "builder": 1}, winner=None)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                await orch._vote(task, team, proposals)

        clarification_events = [e for e in orch.events._events if getattr(e, "type", None) == "user_clarification_requested"]
        self.assertTrue(len(clarification_events) > 0)
        payload = clarification_events[-1].payload
        self.assertIn("tally", payload)
        self.assertIn("valid_candidate_ids", payload)
        self.assertIn("proposal_summaries", payload)
        self.assertEqual(payload["resume_phase"], "vote_resolution")
        self.assertEqual(set(payload["valid_candidate_ids"]), set(roster))
        self.assertIn("question", payload)
        self.assertIn("Architect", payload["question"])
        self.assertEqual(len(payload["proposal_summaries"]), len(roster))
        for summary in payload["proposal_summaries"]:
            self.assertIn("candidate_id", summary)
            self.assertIn("proposal_summary", summary)


class InvalidAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_answer_returns_409_without_changing_state(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"architect": 1, "researcher": 1, "builder": 1}
            state["winner_id"] = None
            state["phase"] = "voted"
            return TallyResult(tally={"architect": 1, "researcher": 1, "builder": 1}, winner=None)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                await orch._vote(task, team, {aid: f"prop-{aid}" for aid in roster})

        self.assertEqual(task.status, "waiting_for_user")

        with self.assertRaises(ValueError) as ctx:
            orch.apply_user_clarification(task.id, "nonexistent_agent")
        self.assertIn("not a valid candidate", str(ctx.exception))
        self.assertEqual(task.status, "waiting_for_user")

    async def test_whitespace_stripped_answer_validated(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        state = orch._state(task.id)
        state["resume_phase"] = "vote_resolution"
        state["vote_resolution"] = {
            "proposals": {aid: f"prop-{aid}" for aid in roster},
            "team_member_ids": list(roster),
            "team_leader_id": "architect",
            "tally": {},
            "candidates": list(roster),
        }
        state["user_clarification"] = {"status": "requested"}
        task.status = "waiting_for_user"

        with self.assertRaises(ValueError):
            orch.apply_user_clarification(task.id, "  unknown  ")
        self.assertEqual(task.status, "waiting_for_user")


class ValidResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_answer_resumes_directly_after_voting(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        state = orch._state(task.id)
        state["resume_phase"] = "vote_resolution"
        state["vote_resolution"] = {
            "proposals": {aid: f"prop-{aid}" for aid in roster},
            "team_member_ids": list(roster),
            "team_leader_id": "architect",
            "tally": {"architect": 1, "researcher": 1, "builder": 1},
            "candidates": list(roster),
        }
        state["user_clarification"] = {"status": "requested"}
        task.status = "waiting_for_user"

        result_task = orch.apply_user_clarification(task.id, "builder")
        self.assertEqual(result_task.status, "running")
        self.assertEqual(state["winner_id"], "builder")

        decision_events = [e for e in orch.events._events if getattr(e, "type", None) == "user_decision_resolved"]
        self.assertEqual(len(decision_events), 1)
        self.assertEqual(decision_events[0].payload["selected_winner"], "builder")
        self.assertEqual(set(decision_events[0].payload["valid_candidate_ids"]), set(roster))

    async def test_continue_after_clarification_skips_pre_vote_phases(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        state = orch._state(task.id)
        state["resume_phase"] = "vote_resolution"
        state["vote_resolution"] = {
            "proposals": {aid: f"prop-{aid}" for aid in roster},
            "team_member_ids": list(roster),
            "team_leader_id": "architect",
            "tally": {"architect": 1, "researcher": 1, "builder": 1},
            "candidates": list(roster),
        }
        state["user_clarification"] = {"status": "requested"}
        task.status = "waiting_for_user"

        orch.apply_user_clarification(task.id, "researcher")

        pre_vote_called: list[str] = []
        async def track_pre_vote(name):
            async def tracker(*args, **kwargs):
                pre_vote_called.append(name)
            return tracker

        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._spawn_child_agent = AsyncMock()
        orch._negotiate = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._collect_proposal_opinions = AsyncMock()
        orch._monitor = AsyncMock()
        orch._record_evaluation_metrics = AsyncMock()
        orch._learn = AsyncMock()
        orch._dissolve = lambda t, tm: None
        orch._compose_answer = lambda t, tm, w, p: f"answer by {w}"
        orch._populate_acceptance_checks = lambda tid, tm: None
        orch._apply_validation_gate = lambda t, tm: "validated"
        orch._record_demo_proof = lambda t, tm: None
        orch._record_task_metrics = lambda t, st: None

        await orch.continue_after_clarification(task.id)

        self.assertEqual(orch._run_agno_team.call_count, 0)
        self.assertEqual(orch._elect_leader.call_count, 0)
        self.assertEqual(orch._spawn_child_agent.call_count, 0)
        self.assertEqual(orch._negotiate.call_count, 0)
        self.assertEqual(orch._delegate_subtasks.call_count, 0)
        self.assertEqual(orch._collect_proposal_opinions.call_count, 0)
        orch._monitor.assert_called_once()
        orch._record_evaluation_metrics.assert_called_once()
        orch._learn.assert_called_once()
        self.assertEqual(task.status, "complete")


class NoDuplicatedPreVotePhasesTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_pre_vote_phases_rerun_on_resume(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        state = orch._state(task.id)
        state["resume_phase"] = "vote_resolution"
        state["vote_resolution"] = {
            "proposals": {aid: f"prop-{aid}" for aid in roster},
            "team_member_ids": list(roster),
            "team_leader_id": "architect",
            "tally": {"architect": 1, "researcher": 1},
            "candidates": list(roster),
        }
        state["user_clarification"] = {"status": "requested"}
        task.status = "waiting_for_user"
        orch.apply_user_clarification(task.id, "architect")

        call_log: list[str] = []

        async def _noop(*a, **kw):
            pass

        for name in ["_run_agno_team", "_elect_leader", "_spawn_child_agent", "_negotiate", "_delegate_subtasks", "_collect_proposal_opinions"]:
            def _make_tracker(n):
                async def _tracker(*a, **kw):
                    call_log.append(n)
                return _tracker
            setattr(orch, name, _make_tracker(name))
        orch._monitor = AsyncMock()
        orch._record_evaluation_metrics = AsyncMock()
        orch._learn = AsyncMock()
        orch._dissolve = lambda t, tm: None
        orch._compose_answer = lambda t, tm, w, p: "answer"
        orch._populate_acceptance_checks = lambda tid, tm: None
        orch._apply_validation_gate = lambda t, tm: "ok"
        orch._record_demo_proof = lambda t, tm: None
        orch._record_task_metrics = lambda t, st: None

        await orch.continue_after_clarification(task.id)

        for name in ["_run_agno_team", "_elect_leader", "_spawn_child_agent", "_negotiate", "_delegate_subtasks", "_collect_proposal_opinions"]:
            self.assertNotIn(name, call_log, f"{name} should not be called during vote_resolution resume")


class MismatchedWinnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_mismatched_tool_winner_derived_from_tally(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        proposals = {aid: f"prop-{aid}" for aid in roster}

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            state = self_orch._state(task.id)
            state["tally"] = {"architect": 3, "researcher": 1, "builder": 1}
            state["winner_id"] = "researcher"
            state["phase"] = "voted"
            return TallyResult(tally={"architect": 3, "researcher": 1, "builder": 1}, winner="researcher")

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                with patch.object(SocietyOrchestrator, "_emit_leader_synthesis", new=AsyncMock()):
                    winner = await orch._vote(task, team, proposals)

        self.assertEqual(winner, "architect")
        self.assertNotEqual(task.status, "waiting_for_user")
        state = orch._state(task.id)
        self.assertEqual(state["winner_id"], "architect")
        self.assertEqual(state["tally"]["architect"], 3)

    async def test_returned_result_without_side_effect(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        proposals = {aid: f"prop-{aid}" for aid in roster}

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(choice="architect", reason="r", confidence=0.8)

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            return TallyResult(tally={"architect": 2, "researcher": 1, "builder": 1}, winner="architect")

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                with patch.object(SocietyOrchestrator, "_emit_leader_synthesis", new=AsyncMock()):
                    winner = await orch._vote(task, team, proposals)

        self.assertEqual(winner, "architect")
        self.assertNotEqual(task.status, "waiting_for_user")
        state = orch._state(task.id)
        self.assertEqual(state["winner_id"], "architect")
        self.assertEqual(state["tally"], {"architect": 3})
        reconciled = [event for event in orch.events._events if event.type == "tally_reconciled_from_ballots"]
        self.assertEqual(len(reconciled), 1)


class NormalUniqueWinnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unique_winner_completes_normally(self) -> None:
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

        self.assertEqual(winner, "builder")
        self.assertNotEqual(task.status, "waiting_for_user")
        self.assertEqual(orch._state(task.id)["winner_id"], "builder")


if __name__ == "__main__":
    unittest.main()
