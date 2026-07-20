from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import AgentProfile, SocietyAgent, SocietyEvent
from society.projections import project_cockpit, project_dossier, project_recap, project_review


def event(event_type: str, payload: dict | None = None, actor: str | None = None, idx: int = 1) -> SocietyEvent:
    """Create one deterministic event for projection tests."""

    return SocietyEvent(
        id=f"event-{idx}",
        task_id="task-1",
        type=event_type,
        message=f"{event_type} happened",
        actor=actor,
        payload=payload or {},
        created_at=f"2026-07-03T12:00:{idx:02d}+00:00",
    )


class ProjectionTests(unittest.TestCase):
    def test_terminal_interruption_projects_consistently_in_cockpit_recap_and_dossier(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("task_interrupted", {"phase": "validation", "reason": "process_restart"}, idx=2),
        ]
        agent = SocietyAgent(id="builder", name="Builder", role="builder", skills=[])

        cockpit = project_cockpit("task-1", events)
        self.assertEqual(cockpit.status, "interrupted")
        self.assertEqual(cockpit.trace_status, "interrupted")
        self.assertEqual(project_recap("task-1", events).status, "interrupted")
        self.assertEqual(project_recap("task-1", events).completion_outcome, "interrupted")
        self.assertEqual(project_dossier(agent, events, [], task_id="task-1").this_run_summary["status"], "interrupted")

    def test_fixed_specialist_execution_projects_skills_graph_artifacts_and_cleanup(self) -> None:
        events = [
            event("specialist_selection_rejected", {"blockers": [{"message": "Unknown dependency."}]}, idx=1),
            event("specialist_selection_accepted", {
                "selection_rationale": "Builder implementation followed by independent validation.",
                "assignments": [
                    {
                        "assignment_id": "build",
                        "template_id": "builder",
                        "template_version": "1",
                        "objective": "Implement the repair.",
                        "capabilities": ["implementation", "artifact_export"],
                        "tool_ids": ["execute_command", "export_artifact"],
                        "skill_ids": ["repository_implementation"],
                        "skill_versions": ["1"],
                        "skill_hashes": ["a" * 64],
                        "depends_on": [],
                        "owned_artifacts": ["src/calc.py"],
                        "acceptance_requirements": ["unit_tests"],
                        "validates_assignment_ids": [],
                    },
                    {
                        "assignment_id": "validate",
                        "template_id": "test_engineer",
                        "template_version": "1",
                        "objective": "Validate the repair.",
                        "capabilities": ["independent_validation"],
                        "tool_ids": ["inspect_artifact", "report_independent_validation"],
                        "skill_ids": ["independent_validation"],
                        "skill_versions": ["1"],
                        "skill_hashes": ["b" * 64],
                        "depends_on": ["build"],
                        "owned_artifacts": [],
                        "acceptance_requirements": ["unit_tests", "independent_validation"],
                        "validates_assignment_ids": ["build"],
                    },
                ],
            }, actor="architect", idx=2),
            event("composition_assignment_materialized", {"assignment_id": "build", "id": "assignment-build"}, idx=3),
            event("work_node_started", {"assignment_id": "build", "node_id": "build"}, idx=4),
            event("agentbay_start_succeeded", {"assignment_id": "build", "node_id": "build", "handle": "redacted"}, idx=5),
            event("agentbay_artifact_exported", {
                "assignment_id": "build",
                "node_id": "build",
                "workspace_relative_path": "src/calc.py",
                "artifact_ref": {
                    "id": "artifact-calc",
                    "sha256": "c" * 64,
                    "size_bytes": 42,
                    "storage_path": "abc/source_calc.py",
                },
            }, idx=6),
            event("composition_assignment_cleanup_completed", {"assignment_id": "build", "node_id": "build"}, idx=7),
            event("work_node_completed", {"assignment_id": "build", "node_id": "build"}, idx=8),
            event("work_node_started", {"assignment_id": "validate", "node_id": "validate"}, idx=9),
            event("local_independent_validation_reported", {"assignment_id": "validate", "node_id": "validate", "passed": True}, idx=10),
            event("composition_assignment_cleanup_completed", {"assignment_id": "validate", "node_id": "validate"}, idx=11),
            event("work_node_completed", {"assignment_id": "validate", "node_id": "validate"}, idx=12),
        ]

        specialist = project_cockpit("task-1", events).specialist_execution

        self.assertIsNotNone(specialist)
        assert specialist is not None
        self.assertEqual(specialist.correction_count, 1)
        self.assertEqual(specialist.assignments[0].skills[0].skill_id, "repository_implementation")
        self.assertEqual(specialist.assignments[1].depends_on, ["build"])
        self.assertEqual(specialist.assignments[0].artifacts[0].status, "exported")
        self.assertEqual(specialist.assignments[0].artifacts[0].validation_status, "passed")
        self.assertEqual(specialist.assignments[0].artifacts[0].artifact_id, "artifact-calc")
        self.assertEqual(specialist.assignments[0].artifacts[0].download_url, "/tasks/task-1/artifacts/artifact-calc")
        self.assertTrue(all(item.sandbox_status == "closed" for item in specialist.assignments))
        self.assertEqual(specialist.development_readiness, "development_gate_passed")

    def test_specialist_artifacts_never_project_windows_absolute_paths(self) -> None:
        """Cockpit keeps workspace-relative names but redacts drive and UNC paths."""

        events = [
            event("specialist_selection_accepted", {"assignments": [{
                "assignment_id": "build", "template_id": "builder", "template_version": "1",
                "objective": "Build safely.", "owned_artifacts": [],
            }]}, idx=1),
            event("agentbay_artifact_exported", {
                "assignment_id": "build", "path": "/workspace/private.py",
                "artifact_ref": {"id": "artifact-drive", "sha256": "a" * 64},
            }, idx=2),
            event("agentbay_artifact_exported", {
                "assignment_id": "build", "path": "\\\\server\\share\\private.txt",
                "artifact_ref": {"id": "artifact-unc", "sha256": "b" * 64},
            }, idx=3),
            event("agentbay_artifact_exported", {
                "assignment_id": "build", "workspace_relative_path": "src/public.py",
                "artifact_ref": {"id": "artifact-relative", "sha256": "c" * 64},
            }, idx=4),
        ]

        specialist = project_cockpit("task-1", events).specialist_execution

        self.assertIsNotNone(specialist)
        assert specialist is not None
        self.assertEqual(
            [artifact.path for artifact in specialist.assignments[0].artifacts],
            ["artifact-drive", "artifact-unc", "src/public.py"],
        )

    def test_completed_run_projects_decision_recap_and_dossier(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a grounded run cockpit"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "task_id": "task-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("tool_call", {"tool_name": "propose", "actor": "builder", "result": {"agent_id": "builder", "proposal": "Ship projection endpoints first.", "rationale": "Backend truth first."}, "success": True}, actor="builder", idx=3),
            event("vote_cast", {"choice": "builder", "reason": "Smallest useful slice.", "confidence": 0.8}, actor="architect", idx=4),
            event("ballots_tallied", {"winner": "builder", "tally": {"builder": 1}}, idx=5),
            event("peer_monitor_report", {"reviewed": "builder", "critique": "Add tests.", "risks": ["uncovered empty state"], "improvements": ["test partial run"]}, actor="critic", idx=6),
            event("meeting_recap", {"plan_changes": ["Backend first"], "unresolved_dissent": ["UI must not fake decisions"], "saved_lessons": ["Project event evidence directly"]}, idx=7),
            event("trust_updated", {"target_agent_id": "builder", "domain": "delivery", "delta": 0.1, "reason": "won vote"}, actor="architect", idx=8),
            event("reputation_updated", {"reputations": {"builder": {"election_score": 1.1}}}, idx=9),
            event("task_metrics", {"turns_count": 4, "proposals_count": 1}, idx=10),
            event("validation_gate_completed", {"passed": True, "checks": []}, actor="architect", idx=11),
            event("task_complete", {"answer": "Done from evidence."}, idx=12),
        ]
        summary = {"status": "complete", "prompt": "Build a grounded run cockpit", "final_answer": "Done from evidence."}

        cockpit = project_cockpit("task-1", events, summary)
        review = project_review("task-1", events)
        recap = project_recap("task-1", events, summary)
        agent = SocietyAgent(id="builder", name="Lin", role="Implementation Engineer", skills=["delivery"], profile=AgentProfile(default_blockers=["unclear deliverables"]))
        dossier = project_dossier(agent, events, [{"task_id": "task-1", "memory": "Projection endpoints worked", "tags": ["lesson"], "mode": "native_agno"}], reputation=1.1)

        self.assertEqual(cockpit.evidence_status, "complete")
        self.assertEqual(cockpit.team.id, "team-1")
        self.assertEqual(review.selected_winner, "builder")
        self.assertEqual(review.ballots[0].choice, "builder")
        self.assertEqual(review.critiques[0].critique, "Add tests.")
        self.assertIn("Backend first", recap.plan_changes)
        self.assertEqual(recap.metrics, {"turns_count": 4, "proposals_count": 1})
        self.assertEqual(dossier.reputation, 1.1)
        self.assertEqual(dossier.task_id, None)

    def test_partial_run_marks_missing_decision_evidence(self) -> None:
        events = [
            event("task_received", {"prompt": "Investigate the UI"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect"], "status": "active"}}, idx=2),
            event("agent_position_stated", {"agent_id": "architect", "phase": "goal_discussion", "stance": "support", "reason": "Scope is clear"}, actor="architect", idx=3),
        ]

        review = project_review("task-1", events)
        recap = project_recap("task-1", events, {"status": "running"})

        self.assertEqual(review.evidence_status, "missing")
        self.assertIn("selected_winner", review.missing_sources)
        self.assertEqual(recap.evidence_status, "missing")
        self.assertIn("task_complete", recap.missing_sources)

    def test_clarification_waiting_projects_blocker(self) -> None:
        events = [
            event("task_received", {"prompt": "Do something ambiguous"}, idx=1),
            event("user_clarification_requested", {"question": "Which target user?"}, actor="architect", idx=2),
        ]

        cockpit = project_cockpit("task-1", events, {"status": "waiting_for_user"})

        self.assertEqual(cockpit.status, "waiting_for_user")
        self.assertEqual(cockpit.blockers[0].required_action, "Which target user?")
        self.assertIn("team_formed", cockpit.missing_sources)

    def test_demo_proof_projects_only_emitted_run_evidence(self) -> None:
        """The judge-facing checklist must expose gaps instead of inferring success."""

        events = [
            event("task_received", {"prompt": "Ship an agent society demo"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect", "researcher", "builder", "critic"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("demo_proof_verified", {
                "verified": False,
                "markers": {
                    "distinct_competencies": True,
                    "evidence_producing_delegation": True,
                    "leader_decision": True,
                    "carried_dissent": False,
                    "final_artifact": True,
                },
                "competency_roles": ["architect", "researcher", "builder", "critic"],
                "evidence_subtask_ids": ["sub-research"],
                "leader_id": "architect",
                "carried_dissent_count": 0,
                "final_artifact_id": "artifact-final-1",
                "missing_markers": ["carried_dissent"],
            }, actor="architect", idx=3),
        ]

        cockpit = project_cockpit("task-1", events, {"status": "complete"})

        self.assertIsNotNone(cockpit.demo_proof)
        self.assertFalse(cockpit.demo_proof.verified)
        self.assertTrue(cockpit.demo_proof.markers["evidence_producing_delegation"])
        self.assertEqual(cockpit.demo_proof.missing_markers, ["carried_dissent"])
        self.assertEqual(cockpit.demo_proof.final_artifact_id, "artifact-final-1")

    def test_proposal_opinions_populate_from_events(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "task_id": "task-1", "member_ids": ["architect", "builder", "critic"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("agent_proposal_submitted", {"proposal_id": "prop-builder-abc123", "agent_id": "builder", "proposal": "Ship the feature with tests.", "rationale": "Smallest useful slice.", "created_from_phase": "debating", "status": "recorded"}, actor="builder", idx=3),
            event("proposal_opinion_recorded", {"proposal_id": "prop-builder-abc123", "agent_id": "critic", "stance": "uncertain", "opinion": "Need more evidence on edge cases.", "confidence": 0.6, "phase": "proposal_review"}, actor="critic", idx=4),
            event("proposal_opinion_recorded", {"proposal_id": "prop-builder-abc123", "agent_id": "architect", "stance": "support", "opinion": "Good direction, keep scope narrow.", "confidence": 0.8, "phase": "proposal_review"}, actor="architect", idx=5),
            event("vote_cast", {"choice": "builder", "reason": "Best approach.", "confidence": 0.9}, actor="architect", idx=6),
            event("ballots_tallied", {"winner": "builder", "tally": {"builder": 1}}, idx=7),
            event("winner_selected", {"winner_agent_id": "builder", "winning_proposal_id": "prop-builder-abc123", "why_won": "Majority vote.", "supporting_votes": ["architect"], "critical_tradeoffs": ["scope"], "dissent_carried": []}, actor="builder", idx=8),
            event("task_complete", {"answer": "Done."}, idx=9),
        ]

        review = project_review("task-1", events)

        self.assertEqual(len(review.proposal_opinions), 2)
        self.assertEqual(review.proposal_opinions[0].proposal_id, "prop-builder-abc123")
        self.assertEqual(review.proposal_opinions[0].agent_id, "critic")
        self.assertEqual(review.proposal_opinions[0].stance, "uncertain")
        self.assertEqual(review.proposal_opinions[0].opinion, "Need more evidence on edge cases.")
        self.assertEqual(review.proposal_opinions[1].agent_id, "architect")
        self.assertEqual(review.proposal_opinions[1].stance, "support")
        self.assertEqual(review.proposals[0].proposal_id, "prop-builder-abc123")

    def test_leader_synthesis_populates_from_events(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "task_id": "task-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("agent_proposal_submitted", {"proposal_id": "prop-builder-xyz", "agent_id": "builder", "proposal": "Ship it.", "rationale": "Fast.", "created_from_phase": "debating"}, actor="builder", idx=3),
            event("vote_cast", {"choice": "builder", "reason": "Best.", "confidence": 0.8}, actor="architect", idx=4),
            event("ballots_tallied", {"winner": "builder", "tally": {"builder": 1}}, idx=5),
            event("winner_selected", {"winner_agent_id": "builder", "winning_proposal_id": "prop-builder-xyz", "why_won": "Won vote.", "supporting_votes": ["architect"], "critical_tradeoffs": [], "dissent_carried": []}, actor="builder", idx=6),
            event("leader_synthesis", {
                "winner_agent_id": "builder",
                "winning_proposal_id": "prop-builder-xyz",
                "winning_proposal_summary": "Ship the feature with tests.",
                "why_won": "Won majority vote with 1 supporting ballot(s).",
                "critical_tradeoffs": ["scope vs speed"],
                "carried_dissent": ["critic: Need more evidence on edge cases"],
                "caveats": ["Edge cases need follow-up"],
                "confidence": 0.75,
                "synthesis_kind": "leader_synthesis",
            }, actor="architect", idx=7),
            event("task_complete", {"answer": "Done."}, idx=8),
        ]

        review = project_review("task-1", events)

        self.assertIsNotNone(review.leader_synthesis)
        self.assertEqual(review.leader_synthesis.winner_agent_id, "builder")
        self.assertEqual(review.leader_synthesis.winning_proposal_id, "prop-builder-xyz")
        self.assertEqual(review.leader_synthesis.winning_proposal_summary, "Ship the feature with tests.")
        self.assertIn("scope vs speed", review.leader_synthesis.critical_tradeoffs)
        self.assertIn("critic: Need more evidence on edge cases", review.leader_synthesis.carried_dissent)
        self.assertEqual(review.leader_synthesis.synthesis_kind, "leader_synthesis")
        self.assertEqual(review.leader_synthesis.confidence, 0.75)

    def test_full_proposal_truth_in_review(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "task_id": "task-1", "member_ids": ["architect", "builder", "critic"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("agent_proposal_submitted", {"proposal_id": "prop-architect-a1", "agent_id": "architect", "proposal": "Decompose into modules.", "rationale": "Clear boundaries.", "created_from_phase": "debating"}, actor="architect", idx=3),
            event("agent_proposal_submitted", {"proposal_id": "prop-builder-b1", "agent_id": "builder", "proposal": "Ship smallest slice first.", "rationale": "Fastest path to value.", "created_from_phase": "debating"}, actor="builder", idx=4),
            event("proposal_opinion_recorded", {"proposal_id": "prop-architect-a1", "agent_id": "builder", "stance": "oppose", "opinion": "Too much upfront design.", "confidence": 0.7, "phase": "proposal_review"}, actor="builder", idx=5),
            event("proposal_opinion_recorded", {"proposal_id": "prop-builder-b1", "agent_id": "architect", "stance": "support", "opinion": "Agree with slicing.", "confidence": 0.8, "phase": "proposal_review"}, actor="architect", idx=6),
            event("proposal_opinion_recorded", {"proposal_id": "prop-builder-b1", "agent_id": "critic", "stance": "uncertain", "opinion": "Need acceptance criteria.", "confidence": 0.5, "phase": "proposal_review"}, actor="critic", idx=7),
            event("vote_cast", {"choice": "builder", "reason": "Best.", "confidence": 0.8}, actor="architect", idx=8),
            event("vote_cast", {"choice": "builder", "reason": "Agreed.", "confidence": 0.7}, actor="critic", idx=9),
            event("ballots_tallied", {"winner": "builder", "tally": {"builder": 2}}, idx=10),
            event("winner_selected", {"winner_agent_id": "builder", "winning_proposal_id": "prop-builder-b1", "why_won": "Won majority.", "supporting_votes": ["architect", "critic"], "critical_tradeoffs": ["scope"], "dissent_carried": ["Need acceptance criteria"]}, actor="builder", idx=11),
            event("leader_synthesis", {
                "winner_agent_id": "builder",
                "winning_proposal_id": "prop-builder-b1",
                "winning_proposal_summary": "Ship smallest slice first.",
                "why_won": "Won majority vote.",
                "critical_tradeoffs": ["scope vs completeness"],
                "carried_dissent": ["critic: Need acceptance criteria"],
                "caveats": ["Follow up on acceptance criteria"],
                "confidence": 0.8,
                "synthesis_kind": "leader_synthesis",
            }, actor="architect", idx=12),
            event("task_complete", {"answer": "Done."}, idx=13),
        ]

        review = project_review("task-1", events)

        self.assertEqual(len(review.proposals), 2)
        self.assertEqual(review.proposals[0].proposal_id, "prop-architect-a1")
        self.assertEqual(review.proposals[1].proposal_id, "prop-builder-b1")
        self.assertEqual(len(review.proposal_opinions), 3)
        self.assertEqual(review.selected_winner, "builder")
        self.assertEqual(review.selected_proposal_id, "prop-builder-b1")
        self.assertIsNotNone(review.winner_rationale)
        self.assertIsNotNone(review.leader_synthesis)
        self.assertEqual(review.leader_synthesis.winning_proposal_summary, "Ship smallest slice first.")
        self.assertIn("critic: Need acceptance criteria", review.leader_synthesis.carried_dissent)

    def test_rich_delegation_projects_evidence_and_outcome(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "task_id": "task-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("delegation_assigned", {
                "subtask_id": "sub-1",
                "agent_id": "builder",
                "assigned_by": "architect",
                "objective": "Implement the API endpoint",
                "why_assigned": "Builder owns delivery",
                "done_criteria": ["endpoint returns 200", "tests pass"],
                "blocking_if_missing": True,
                "provenance": "working_brief",
                "status": "assigned",
            }, actor="architect", idx=3),
            event("delegation_reported", {
                "subtask_id": "sub-1",
                "agent_id": "builder",
                "status": "completed",
                "result_summary": "Endpoint implemented with tests",
                "outcome_status": "completed",
                "outcome_summary": "API endpoint delivered",
                "evidence_refs": ["context7:fastapi-routing", "memory:lesson-42"],
                "provenance": "agent_report",
                "blockers": [],
            }, actor="builder", idx=4),
            event("task_complete", {"answer": "Done."}, idx=5),
        ]

        cockpit = project_cockpit("task-1", events, {"status": "complete"})
        recap = project_recap("task-1", events, {"status": "complete"})

        self.assertEqual(len(cockpit.delegation_summary), 1)
        delegation = cockpit.delegation_summary[0]
        self.assertEqual(delegation.subtask_id, "sub-1")
        self.assertEqual(delegation.why_assigned, "Builder owns delivery")
        self.assertEqual(delegation.done_criteria, ["endpoint returns 200", "tests pass"])
        self.assertTrue(delegation.blocking_if_missing)
        self.assertEqual(delegation.provenance, "working_brief")
        self.assertEqual(delegation.result_summary, "Endpoint implemented with tests")
        self.assertEqual(delegation.outcome_status, "completed")
        self.assertEqual(delegation.outcome_summary, "API endpoint delivered")
        self.assertEqual(delegation.evidence_refs, ["context7:fastapi-routing", "memory:lesson-42"])
        self.assertEqual(len(recap.delegation_outcomes), 1)
        self.assertEqual(recap.delegation_outcomes[0].evidence_refs, ["context7:fastapi-routing", "memory:lesson-42"])

    def test_failure_projects_real_missing_inputs_and_evidence(self) -> None:
        events = [
            event("task_received", {"prompt": "Build something"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect"], "status": "active"}}, idx=2),
            event("working_brief_finalized", {
                "summary": "Build something",
                "open_questions": ["What is the target platform?"],
                "blocked_items": ["Auth not specified"],
                "unresolved_dissent": [],
            }, idx=3),
            event("readiness_vote_tallied", {"blockers": ["architect: Need scope clarity"]}, idx=4),
            event("delegation_reported", {
                "subtask_id": "sub-1",
                "agent_id": "architect",
                "status": "blocked",
                "blockers": ["Missing API spec"],
                "result_summary": "Cannot proceed",
            }, actor="architect", idx=5),
            event("validation_gate_completed", {"passed": False, "failed_checks": [{"check": "winner_selected", "reason": "no winner"}]}, idx=6),
            event("run_failed", {
                "phase": "debate",
                "blocking_reason": "Governance tool failure",
                "missing_inputs": ["open question: What is the target platform?", "blocked item: Auth not specified", "readiness blocker: architect: Need scope clarity"],
                "missing_evidence": ["subtask sub-1: Missing API spec", "failed check winner_selected: no winner"],
                "system_error": "tool timeout",
                "recoverable": False,
            }, idx=7),
        ]

        cockpit = project_cockpit("task-1", events, {"status": "failed"})

        self.assertIsNotNone(cockpit.failure)
        failure = cockpit.failure
        self.assertEqual(failure.phase, "debate")
        self.assertTrue(len(failure.missing_inputs) > 0)
        self.assertTrue(len(failure.missing_evidence) > 0)
        self.assertIn("open question: What is the target platform?", failure.missing_inputs)
        self.assertIn("readiness blocker: architect: Need scope clarity", failure.missing_inputs)
        self.assertIn("subtask sub-1: Missing API spec", failure.missing_evidence)
        self.assertIn("failed check winner_selected: no winner", failure.missing_evidence)

    def test_complete_with_caveats_when_validation_fails(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("working_brief_finalized", {
                "summary": "Build a feature",
                "unresolved_dissent": ["Critic: edge cases need follow-up"],
            }, idx=3),
            event("validation_gate_completed", {"passed": False, "failed_checks": [{"check": "no_blocked_subtasks", "reason": "1 subtask(s) have blockers"}]}, idx=4),
            event("task_complete", {"answer": "Done with caveats."}, idx=5),
        ]

        recap = project_recap("task-1", events, {"status": "complete"})

        self.assertEqual(recap.completion_outcome, "complete_with_caveats")

    def test_complete_with_caveats_when_blocked_subtasks(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("delegation_reported", {
                "subtask_id": "sub-1",
                "agent_id": "builder",
                "status": "blocked",
                "blockers": ["Missing dependency"],
            }, actor="builder", idx=3),
            event("task_complete", {"answer": "Done with caveats."}, idx=4),
        ]

        recap = project_recap("task-1", events, {"status": "complete"})

        self.assertEqual(recap.completion_outcome, "complete_with_caveats")

    def test_complete_with_caveats_from_leader_synthesis_caveats(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("leader_synthesis", {
                "winner_agent_id": "builder",
                "winning_proposal_id": "prop-1",
                "winning_proposal_summary": "Ship it.",
                "why_won": "Won vote.",
                "caveats": ["Edge cases need follow-up"],
                "synthesis_kind": "leader_synthesis",
            }, actor="architect", idx=3),
            event("task_complete", {"answer": "Done."}, idx=4),
        ]

        recap = project_recap("task-1", events, {"status": "complete"})

        self.assertEqual(recap.completion_outcome, "complete_with_caveats")

    def test_clean_complete_is_not_caveated(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("validation_gate_completed", {"passed": True, "checks": [], "failed_checks": []}, idx=3),
            event("task_complete", {"answer": "Done."}, idx=4),
        ]

        recap = project_recap("task-1", events, {"status": "complete"})

        self.assertEqual(recap.completion_outcome, "complete")

    def test_objection_events_project_in_review_regardless_of_trace_flag(self) -> None:
        events = [
            event("task_received", {"prompt": "Build a feature"}, idx=1),
            event("team_formed", {"team": {"id": "team-1", "member_ids": ["architect", "builder", "critic"], "leader_id": "architect", "status": "active"}}, idx=2),
            event("agent_objection_registered", {
                "agent_id": "critic",
                "severity": "critical",
                "objection": "Missing validation gate",
                "resolution_condition": "Add acceptance check",
                "blocks_execution": True,
            }, actor="critic", idx=3),
            event("agent_objection_registered", {
                "agent_id": "builder",
                "severity": "medium",
                "objection": "Scope could expand",
                "resolution_condition": "Confirm scope boundary",
                "blocks_execution": False,
            }, actor="builder", idx=4),
            event("task_complete", {"answer": "Done."}, idx=5),
        ]

        review = project_review("task-1", events)

        self.assertEqual(len(review.blocking_objections), 1)
        self.assertIn("Missing validation gate", review.blocking_objections)
        self.assertEqual(len(review.non_blocking_dissent), 1)
        self.assertIn("Scope could expand", review.non_blocking_dissent)
        self.assertTrue(len(review.unresolved_dissent) >= 2)

    def test_final_synthesis_is_the_only_projected_winner(self) -> None:
        """Later decision evidence supersedes a provisional tally everywhere."""

        events = [
            event("agent_proposal_submitted", {"agent_id": "builder", "proposal_id": "build-1", "proposal": "Build it."}, actor="builder", idx=1),
            event("agent_proposal_submitted", {"agent_id": "researcher", "proposal_id": "research-1", "proposal": "Research it."}, actor="researcher", idx=2),
            event("ballots_tallied", {"winner": "researcher"}, idx=3),
            event("winner_selected", {"winner_agent_id": "researcher", "winning_proposal_id": "research-1", "why_won": "Initial tally."}, idx=4),
            event("leader_synthesis", {
                "winner_agent_id": "builder", "winning_proposal_id": "build-1",
                "winning_proposal_summary": "Build it.", "why_won": "Final evidence favored delivery.",
            }, actor="leader", idx=5),
        ]

        review = project_review("task-1", events)

        self.assertEqual(review.selected_winner, "builder")
        self.assertEqual(review.selected_proposal_id, "build-1")
        self.assertEqual(review.leader_synthesis.winner_agent_id, review.selected_winner)

    def test_verified_validation_clears_only_stale_proof_gap_claims(self) -> None:
        """Review, Recap, and Dossier use validator evidence over old prose."""

        claims = [
            "Missing artifact evidence", "Dual viewport validation is missing",
            "Validation output was truncated", "Accessibility validation is missing",
        ]
        events = [
            event("agentbay_artifact_exported", {"assignment_id": "build", "path": "src/page.html"}, idx=1),
            event("readiness_vote_tallied", {"blockers": claims}, idx=2),
            event("agent_objection_registered", {"objection": claims[0], "blocks_execution": True}, actor="critic", idx=3),
            event("meeting_recap", {"unresolved_dissent": claims}, idx=4),
            event("local_independent_validation_reported", {
                "passed": True,
                "checks": ["Desktop and mobile viewport checks passed", "Accessibility checks passed"],
            }, idx=5),
            event("validation_gate_completed", {"passed": False, "failed_checks": [{"check": "artifact", "reason": claims[0]}]}, idx=6),
            event("validation_gate_completed", {"passed": True, "failed_checks": []}, idx=7),
            event("acceptance_evidence_evaluated", {"terminal_status": "complete"}, idx=8),
            event("task_complete", {"answer": "Old proposal prose claimed a blocker."}, idx=9),
        ]
        agent = SocietyAgent(
            id="critic", name="Critic", role="reviewer", skills=[],
            profile=AgentProfile(default_blockers=claims),
        )

        cockpit = project_cockpit("task-1", events)
        review = project_review("task-1", events)
        recap = project_recap("task-1", events)
        dossier = project_dossier(agent, events, [], task_id="task-1")

        self.assertEqual(cockpit.blockers, [])
        self.assertEqual(review.blocking_objections, [])
        self.assertEqual(review.unresolved_dissent, [])
        self.assertIn("durable_artifact", [item.type for item in review.supporting_artifacts])
        self.assertEqual(recap.dissents, [])
        self.assertEqual(recap.completion_outcome, "complete")
        self.assertNotIn("default blockers:", " ".join(dossier.behavioral_tendencies))


if __name__ == "__main__":
    unittest.main()
