from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.debate import ChallengeRecord, ProposalRecord, RevisionRecord
from society.schemas.governance import VoteDecision
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
            skills=["risk analysis"] if agent_id == "critic" else ["test"],
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
        "challenges": [],
        "revisions": {},
        "artifacts": [],
    }
    return task, team


class RevisionPathNameErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_change_summary_defined_before_shared_artifact_revision(self) -> None:
        settings = _make_settings(native_debate_enabled=True)
        orch = _make_orchestrator(settings)
        roster = ["critic", "architect"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)

        call_count = 0

        async def fake_gov_tool(self_orch, *, task, actor_identity, tool_func, tool_name, schema_class, prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if schema_class is ChallengeRecord:
                return ChallengeRecord(
                    challenger="critic",
                    target="architect",
                    objection="Weak assumption",
                    suggested_revision="Tighten scope",
                    round=0,
                )
            if schema_class is RevisionRecord:
                return RevisionRecord(
                    agent_id="architect",
                    revised_proposal="prop-architect revised",
                    changes=["Tightened scope", "Added acceptance check"],
                    round=0,
                )
            return None

        with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
            proposals = {"critic": "prop-critic", "architect": "prop-architect"}
            await orch._run_debate_revision_round(task, team, proposals)

        state = orch._state(task.id)
        revisions = state.get("shared_artifact_revisions", [])
        self.assertTrue(len(revisions) > 0, "shared_artifact_revisions should have at least one entry")
        last_revision = revisions[-1]
        self.assertIn("change_summary", last_revision)
        self.assertTrue(len(last_revision["change_summary"]) > 0)


class NativeUniqueWinnerPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_unique_winner_sets_phase_voted(self) -> None:
        settings = _make_settings()
        orch = _make_orchestrator(settings)
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"
        orch._state(task.id)["phase"] = "voting"

        vote_map = {
            "architect": "builder",
            "researcher": "builder",
            "builder": "builder",
        }

        async def fake_isolated(self_orch, *, actor_identity, **kwargs):
            return VoteDecision(
                choice=vote_map[actor_identity.id],
                reason=f"{actor_identity.id} voted",
                confidence=0.8,
            )

        async def fake_gov_tool(self_orch, *, tool_func, tool_name, schema_class, **kwargs):
            from society.schemas.voting import TallyResult
            from collections import Counter
            state = self_orch._state(task.id)
            ballots = state.get("ballots", [])
            votes = Counter(b["choice"] for b in ballots)
            tally = dict(votes.most_common())
            winner = votes.most_common(1)[0][0] if votes else None
            state["tally"] = tally
            state["winner_id"] = winner
            return TallyResult(tally=tally, winner=winner)

        with patch.object(SocietyOrchestrator, "_run_governance_tool_isolated", new=fake_isolated):
            with patch.object(SocietyOrchestrator, "_run_governance_tool", new=fake_gov_tool):
                with patch.object(SocietyOrchestrator, "_emit_leader_synthesis", new=AsyncMock()):
                    winner = await orch._vote(task, team, dict(zip(roster, [f"prop-{r}" for r in roster])))

        self.assertEqual(winner, "builder")
        self.assertEqual(orch._state(task.id)["winner_id"], "builder")
        self.assertEqual(orch._state(task.id)["phase"], "voted")


class ProposalArgumentNormalizationTests(unittest.TestCase):
    """Protect governance runs from common model-generated argument shape errors."""

    def test_structured_proposal_and_boolean_supports_are_normalized(self) -> None:
        record = ProposalRecord.model_validate(
            {
                "agent_id": "researcher",
                "proposal": {"selected_ids": ["CL01"], "deployment_gap": "CL03"},
                "rationale": "Evidence-backed recommendation",
                "supports": True,
            }
        )

        self.assertEqual(
            record.proposal,
            '{"deployment_gap": "CL03", "selected_ids": ["CL01"]}',
        )
        self.assertEqual(record.supports, [])


class BenchmarkEquivalentTieTests(unittest.TestCase):
    """Keep autonomous benchmark tie-breaking limited to identical answers."""

    def test_identical_json_tie_prefers_tied_team_leader(self) -> None:
        orch = _make_orchestrator(_make_settings(BENCHMARK_SUITE_TOOLS_ENABLED=True))
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        _, team = _make_task_and_team(orch, roster)
        team.leader_id = "researcher"

        winner = orch._benchmark_equivalent_tie_winner(
            team,
            {"architect": '{"selected_ids":["O2"]}', "researcher": '{ "selected_ids": ["O2"] }'},
            roster,
        )

        self.assertEqual(winner, "researcher")

    def test_different_tied_proposals_still_require_user_resolution(self) -> None:
        orch = _make_orchestrator(_make_settings(BENCHMARK_SUITE_TOOLS_ENABLED=True))
        roster = ["architect", "researcher"]
        orch.agents = _make_agents(roster)
        _, team = _make_task_and_team(orch, roster)

        winner = orch._benchmark_equivalent_tie_winner(
            team,
            {"architect": "option A", "researcher": "option B"},
            roster,
        )

        self.assertIsNone(winner)


if __name__ == "__main__":
    unittest.main()
