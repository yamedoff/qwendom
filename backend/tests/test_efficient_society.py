"""Regression tests for the bounded proposal cross-review matrix."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.debate import ProposalOpinionRecord, ProposalRecord
from society.schemas.conversation import GoalDiscussionStatement


def agent(agent_id: str) -> SocietyAgent:
    return SocietyAgent(
        id=agent_id, name=agent_id, role=agent_id, skills=[agent_id], profile=AgentProfile()
    )


class EfficientSocietyTests(unittest.IsolatedAsyncioTestCase):
    async def test_lean_negotiation_uses_leader_and_one_critic_only(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch.settings = SimpleNamespace()
        ids = ["architect", "researcher", "builder", "critic"]
        orch.agents = {agent_id: agent(agent_id) for agent_id in ids}
        orch.agents["critic"].skills = ["risk analysis"]
        state = {
            "phase": "delegation",
            "metrics": {"debate_rounds": 0},
            "subtasks": [{
                "agent_id": "researcher", "subtask": "verify", "result": "evidence",
                "result_summary": "verified", "evidence_refs": ["E1"], "status": "completed",
            }],
            "proposals": {},
        }
        orch._state = lambda _task_id: state
        orch._safe_list_events = lambda _task_id: [
            SimpleNamespace(
                type="local_independent_validation_reported",
                payload={"passed": True, "inspected_artifact_refs": ["agentbay/run/index.html"]},
            )
        ]
        orch._register_artifact = lambda *args, **kwargs: None
        emitted: list[str] = []
        orch._emit = lambda _task_id, event_type, *args, **kwargs: emitted.append(event_type)
        calls: list[str] = []
        prompts: list[str] = []

        async def governance(**kwargs):
            actor_id = kwargs["actor_identity"].id
            calls.append(actor_id)
            prompts.append(kwargs["prompt"])
            return ProposalRecord(
                proposal_id=f"p-{actor_id}", agent_id=actor_id,
                proposal=f"answer from {actor_id}", rationale=f"reason from {actor_id}",
            )

        orch._run_governance_tool = governance
        task = TaskRun(prompt="produce answer", status="running")
        team = Team(
            task_id=task.id, member_ids=ids, voter_ids=ids,
            leader_id="architect", status="active",
        )

        proposals = await orch._negotiate_efficient(task, team)

        self.assertEqual(calls, ["architect", "critic"])
        self.assertEqual(set(proposals), {"architect", "critic"})
        self.assertEqual(state["critique_source"], "lean_counterproposal")
        self.assertIn("peer_monitor_report", emitted)
        self.assertIn("Authoritative runtime evidence", prompts[0])
        self.assertIn("local_independent_validation_reported", prompts[0])

    async def test_discussion_response_supplies_real_readiness_ballot(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        state = {
            "discussion_round_count": 1,
            "goal_discussions": [
                GoalDiscussionStatement(
                    round=1,
                    agent_id="critic",
                    interpretation="The task is clear",
                    unique_contribution="Proceed with an explicit quality check",
                    ready=True,
                ).model_dump()
            ],
        }
        orch._state = lambda _task_id: state
        task = TaskRun(prompt="efficient readiness", status="running")
        team = Team(task_id=task.id, member_ids=["critic"], voter_ids=["critic"])

        ballots = orch._combined_discussion_readiness_ballots(task, team, attempt=1)

        self.assertEqual(len(ballots), 1)
        self.assertEqual(ballots[0][0], "critic")
        self.assertTrue(ballots[0][1].ready)
        self.assertEqual(ballots[0][1].reason, "Proceed with an explicit quality check")

    async def test_redundant_post_readiness_rounds_are_skipped(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch.settings = SimpleNamespace(
            social_tools_enabled=True, efficient_society_enabled=True,
        )
        state = {"working_brief": {"summary": "ready"}}
        orch._state = lambda _task_id: state
        emitted: list[str] = []
        orch._emit = lambda _task_id, event_type, *args, **kwargs: emitted.append(event_type)
        task = TaskRun(prompt="efficient task", status="running")
        team = Team(task_id=task.id, member_ids=["architect"], voter_ids=["architect"])

        await orch._collect_working_brief_positions(task, team)
        await orch._collect_private_notes(task, team)

        self.assertEqual(emitted, ["efficient_phase_skipped", "efficient_phase_skipped"])

    async def test_computed_metrics_do_not_spend_model_calls(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch.settings = SimpleNamespace(
            evaluation_metrics_enabled=True, llm_enabled=True,
            efficient_society_enabled=True,
        )
        orch.agents = {"architect": agent("architect")}
        state = {"critique": {"confidence": 0.8}, "subtasks": [], "proposals": {"architect": {}}, "failed_checks": []}
        orch._state = lambda _task_id: state

        async def unexpected(**kwargs):
            raise AssertionError("computed metrics must not call Qwen")

        orch._run_governance_tool = unexpected
        task = TaskRun(prompt="record metrics", status="running")
        team = Team(
            task_id=task.id, member_ids=["architect"], voter_ids=["architect"],
            leader_id="architect", status="active",
        )

        await orch._record_evaluation_metrics(task, team, "architect")

        self.assertEqual(len(state["evaluation_metrics"]), 5)

    async def test_efficient_mode_collects_one_cross_review_per_member(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch.settings = SimpleNamespace(
            llm_enabled=True, native_debate_enabled=True,
            readiness_concurrency=4, efficient_society_enabled=True,
        )
        ids = ["architect", "researcher", "builder", "critic"]
        orch.agents = {agent_id: agent(agent_id) for agent_id in ids}
        state = {"proposal_id_map": {agent_id: f"p-{agent_id}" for agent_id in ids}}
        orch._state = lambda _task_id: state
        calls: list[str] = []

        async def isolated(**kwargs):
            actor = kwargs["actor_identity"].id
            calls.append(actor)
            proposal_id = kwargs["derived_session_id"].split(":opinion:", 1)[1].split(":", 2)[1]
            return ProposalOpinionRecord(
                agent_id=actor, proposal_id=proposal_id,
                opinion="bounded cross-review", stance="neutral",
            )

        orch._run_governance_tool_isolated = isolated
        orch._emit_tool_call = lambda *args, **kwargs: None
        orch._emit = lambda *args, **kwargs: None
        task = TaskRun(prompt="compare proposals", status="running")
        team = Team(task_id=task.id, member_ids=ids, voter_ids=ids, status="active")

        await orch._collect_proposal_opinions(
            task, team, {agent_id: f"proposal by {agent_id}" for agent_id in ids}
        )

        self.assertEqual(calls, ids)
        self.assertEqual(len(state["proposal_opinions"]), len(ids))


if __name__ == "__main__":
    unittest.main()
