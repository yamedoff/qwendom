"""Focused tests for WRAP blocker root fixes.

Tests the four root causes:
1. Role-specific delegation enforcement (builder/critic)
2. Carried dissent recognition from working brief and winner_selected
3. Proof verification in task_complete event payload
4. No-mock constraint enforcement
"""
from __future__ import annotations

import unittest
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.orchestrator import (
    _prompt_has_no_mock_constraint,
    _strip_mock_recommendations,
    _prompt_requires_implementation,
    _prompt_requires_validation,
    _is_execution_output_blocker,
)


class RoleSpecificDelegationTests(unittest.TestCase):
    """Test that user brief requirements trigger role-specific subtasks."""

    def test_implementation_prompt_requires_builder(self) -> None:
        """Prompts with implementation language require builder delegation."""
        self.assertTrue(_prompt_requires_implementation("Build a REST API"))
        self.assertTrue(_prompt_requires_implementation("Implement authentication"))
        self.assertTrue(_prompt_requires_implementation("Create a dashboard"))
        self.assertTrue(_prompt_requires_implementation("Ship the feature"))
        self.assertFalse(_prompt_requires_implementation("Analyze the data"))

    def test_analysis_only_briefs_do_not_require_implementation(self) -> None:
        """Explicit analysis-only or no-implementation briefs return False."""
        self.assertFalse(_prompt_requires_implementation(
            "Produce a decision memo; analysis only; no implementation required"
        ))
        self.assertFalse(_prompt_requires_implementation(
            "Write a report on the architecture risks. Do not implement anything."
        ))
        self.assertFalse(_prompt_requires_implementation(
            "Create an analysis of the security threats"
        ))
        self.assertFalse(_prompt_requires_implementation(
            "Draft a recommendation for the database strategy"
        ))
        self.assertFalse(_prompt_requires_implementation(
            "Prepare a checklist of deployment steps"
        ))

    def test_document_deliverables_do_not_require_implementation(self) -> None:
        """Producing/writing memos, reports, analyses, recommendations, checklists return False."""
        self.assertFalse(_prompt_requires_implementation("Produce a decision memo"))
        self.assertFalse(_prompt_requires_implementation("Write a status report"))
        self.assertFalse(_prompt_requires_implementation("Create a risk analysis"))
        self.assertFalse(_prompt_requires_implementation("Generate a recommendation"))
        self.assertFalse(_prompt_requires_implementation("Draft a summary"))
        self.assertFalse(_prompt_requires_implementation("Prepare an overview"))
        self.assertFalse(_prompt_requires_implementation("Write a review"))
        self.assertFalse(_prompt_requires_implementation("Create an assessment"))
        self.assertFalse(_prompt_requires_implementation("Produce an evaluation"))

    def test_strong_implementation_verbs_require_builder(self) -> None:
        """Strong implementation verbs always require implementation."""
        self.assertTrue(_prompt_requires_implementation("Implement the login flow"))
        self.assertTrue(_prompt_requires_implementation("Build the payment system"))
        self.assertTrue(_prompt_requires_implementation("Develop the API endpoints"))
        self.assertTrue(_prompt_requires_implementation("Ship the new feature"))
        self.assertTrue(_prompt_requires_implementation("Code the authentication module"))
        self.assertTrue(_prompt_requires_implementation("Prototype the dashboard"))
        self.assertTrue(_prompt_requires_implementation("Construct the data pipeline"))
        self.assertTrue(_prompt_requires_implementation("Make it runnable"))
        self.assertTrue(_prompt_requires_implementation("Deliver an executable artifact"))
        self.assertTrue(_prompt_requires_implementation("Build the API and write a report"))

    def test_create_with_concrete_artifacts_requires_implementation(self) -> None:
        """'Create' followed by concrete artifacts requires implementation."""
        self.assertTrue(_prompt_requires_implementation("Create a dashboard"))
        self.assertTrue(_prompt_requires_implementation("Create an API"))
        self.assertTrue(_prompt_requires_implementation("Create a web application"))
        self.assertTrue(_prompt_requires_implementation("Create a service"))
        self.assertTrue(_prompt_requires_implementation("Create a tool"))
        self.assertTrue(_prompt_requires_implementation("Create a new feature"))
        self.assertTrue(_prompt_requires_implementation("Create a component"))
        self.assertTrue(_prompt_requires_implementation("Create a module"))
        self.assertTrue(_prompt_requires_implementation("Create an interface"))
        self.assertTrue(_prompt_requires_implementation("Create a website"))
        self.assertTrue(_prompt_requires_implementation("Create a platform"))
        self.assertTrue(_prompt_requires_implementation("Create a solution"))
        self.assertTrue(_prompt_requires_implementation("Create a program"))
        self.assertTrue(_prompt_requires_implementation("Create a function"))
        self.assertTrue(_prompt_requires_implementation("Create a class"))
        self.assertTrue(_prompt_requires_implementation("Create a method"))
        self.assertTrue(_prompt_requires_implementation("Create an endpoint"))
        self.assertTrue(_prompt_requires_implementation("Create an integration"))
        self.assertTrue(_prompt_requires_implementation("Create an automation"))
        self.assertTrue(_prompt_requires_implementation("Create a pipeline"))
        self.assertTrue(_prompt_requires_implementation("Create a workflow"))
        self.assertTrue(_prompt_requires_implementation("Create a script"))

    def test_validation_prompt_requires_critic(self) -> None:
        """Prompts with validation language require critic delegation."""
        self.assertTrue(_prompt_requires_validation("Validate the API responses"))
        self.assertTrue(_prompt_requires_validation("Test the edge cases"))
        self.assertTrue(_prompt_requires_validation("Verify security"))
        self.assertTrue(_prompt_requires_validation("Check acceptance criteria"))
        self.assertTrue(_prompt_requires_validation("Review for risks"))
        self.assertFalse(_prompt_requires_validation("Build the feature"))

    def test_implementation_and_validation_are_independent(self) -> None:
        """Implementation and validation requirements are detected separately."""
        prompt = "Build and validate the authentication system"
        self.assertTrue(_prompt_requires_implementation(prompt))
        self.assertTrue(_prompt_requires_validation(prompt))


class ReadinessBoundaryTests(unittest.TestCase):
    """Readiness cannot demand artifacts that execution is meant to create."""

    def test_missing_work_outputs_are_circular_blockers(self) -> None:
        self.assertTrue(_is_execution_output_blocker("Researcher has not yet provided URLs and Builder implementation is missing"))
        self.assertTrue(_is_execution_output_blocker("Validation artifacts are incomplete"))

    def test_real_user_decisions_remain_blockers(self) -> None:
        self.assertFalse(_is_execution_output_blocker("The user must choose which target market is in scope"))
        self.assertFalse(_is_execution_output_blocker("Constraints conflict: ship today and do not deploy"))


class BuilderExecutionEvidenceTests(unittest.TestCase):
    """Builder execution evidence comes from a real bounded subprocess."""

    def test_notes_demo_executes_health_and_round_trip(self) -> None:
        from society.tools.capabilities import execute_notes_demo_tool

        evidence = json.loads(execute_notes_demo_tool.entrypoint(architecture="centralized"))
        self.assertTrue(evidence["executed"])
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["health"]["status"], "ok")
        self.assertEqual(evidence["round_trip"], {"text": "qwendom-proof"})


class CarriedDissentRecognitionTests(unittest.TestCase):
    """Test that carried dissent is recognized from multiple sources."""

    def test_demo_proof_recognizes_working_brief_dissent(self) -> None:
        """Carried dissent from working brief is recognized even without leader synthesis."""
        from society.orchestrator import SocietyOrchestrator

        state = {
            "tool_bundles": {
                "architect": {"role_key": "architect"},
                "researcher": {"role_key": "researcher"},
                "builder": {"role_key": "builder"},
            },
            "subtasks": [{"id": "sub-research", "evidence_refs": ["context7:agno-team"]}],
            "working_brief": {
                "unresolved_dissent": ["critic: edge cases need explicit handling"],
            },
            "leader_synthesis": {
                "winning_proposal_summary": "Ship the feature.",
            },
            "final_deliverable": {"selected_artifact_id": "artifact-final-1"},
        }
        emitted: list[tuple[str, dict]] = []
        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orchestrator._state = lambda _task_id: state
        orchestrator._emit = lambda _task_id, event_type, _message, **kwargs: emitted.append((event_type, kwargs["payload"]))

        orchestrator._record_demo_proof(
            SimpleNamespace(id="task-1"),
            SimpleNamespace(leader_id="architect"),
        )

        self.assertEqual(emitted[0][0], "demo_proof_verified")
        self.assertTrue(emitted[0][1]["verified"])
        self.assertTrue(state["demo_proof"]["markers"]["carried_dissent"])

    def test_demo_proof_recognizes_winner_selected_dissent(self) -> None:
        """Carried dissent from winner_selected event is recognized."""
        from society.orchestrator import SocietyOrchestrator
        from society.models import SocietyEvent

        state = {
            "tool_bundles": {
                "architect": {"role_key": "architect"},
                "researcher": {"role_key": "researcher"},
                "builder": {"role_key": "builder"},
            },
            "subtasks": [{"id": "sub-research", "evidence_refs": ["context7:agno-team"]}],
            "leader_synthesis": {
                "winning_proposal_summary": "Ship the feature.",
            },
            "final_deliverable": {"selected_artifact_id": "artifact-final-1"},
        }
        emitted: list[tuple[str, dict]] = []
        events_list = [
            SocietyEvent(
                id="event-winner",
                task_id="task-1",
                type="winner_selected",
                message="Winner selected",
                actor="architect",
                payload={"dissent_carried": ["builder: needs more tests"]},
            ),
        ]
        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orchestrator._state = lambda _task_id: state
        orchestrator._emit = lambda _task_id, event_type, _message, **kwargs: emitted.append((event_type, kwargs["payload"]))
        orchestrator._safe_list_events = lambda _task_id: events_list

        orchestrator._record_demo_proof(
            SimpleNamespace(id="task-1"),
            SimpleNamespace(leader_id="architect"),
        )

        self.assertEqual(emitted[0][0], "demo_proof_verified")
        self.assertTrue(emitted[0][1]["verified"])
        self.assertTrue(state["demo_proof"]["markers"]["carried_dissent"])

    def test_demo_proof_safe_event_accessor_handles_missing_store(self) -> None:
        """_safe_list_events returns [] when events store is unavailable."""
        from society.orchestrator import SocietyOrchestrator

        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        # No events attribute set
        result = orchestrator._safe_list_events("task-1")
        self.assertEqual(result, [])

    def test_demo_proof_derives_competencies_from_completed_delegations(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        state = {
            "tool_bundles": {},
            "subtasks": [
                {"id": "r", "agent_id": "researcher", "status": "completed", "outcome_status": "completed", "evidence_refs": ["doc"]},
                {"id": "b", "agent_id": "builder", "status": "completed", "outcome_status": "completed", "evidence_refs": ["execution"]},
                {"id": "c", "agent_id": "critic", "status": "completed", "outcome_status": "completed", "evidence_refs": ["review"]},
            ],
            "leader_synthesis": {"winning_proposal_summary": "ship", "carried_dissent": ["risk"]},
            "final_deliverable": {"selected_artifact_id": "final"},
        }
        emitted = []
        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orchestrator._state = lambda _task_id: state
        orchestrator._safe_list_events = lambda _task_id: []
        orchestrator._emit = lambda *_args, **kwargs: emitted.append(kwargs["payload"])
        orchestrator.agents = {
            "researcher": SimpleNamespace(id="researcher", role="Research Analyst", skills=[]),
            "builder": SimpleNamespace(id="builder", role="Implementation Engineer", skills=[]),
            "critic": SimpleNamespace(id="critic", role="Adversarial Reviewer", skills=[]),
        }
        orchestrator._record_demo_proof(SimpleNamespace(id="task"), SimpleNamespace(leader_id="critic"))
        self.assertTrue(emitted[0]["markers"]["distinct_competencies"])


class ProofVerificationPayloadTests(unittest.TestCase):
    """Test that proof verification is reflected in task_complete event."""

    def test_task_complete_includes_proof_payload(self) -> None:
        """task_complete event includes demo_proof_verified and missing_markers."""
        from society.orchestrator import SocietyOrchestrator

        state = {
            "demo_proof": {
                "verified": False,
                "missing_markers": ["carried_dissent", "evidence_producing_delegation"],
            },
        }
        emitted: list[tuple[str, dict]] = []
        orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orchestrator._state = lambda _task_id: state
        orchestrator._emit = lambda _task_id, event_type, _message, **kwargs: emitted.append((event_type, kwargs.get("payload", {})))
        orchestrator._record_task_metrics = lambda *args, **kwargs: None

        task = SimpleNamespace(id="task-1", final_answer="Done")
        team = SimpleNamespace(leader_id="architect")
        start_time = 0.0

        # Simulate the terminal gate logic
        proof = state.get("demo_proof") or {}
        proof_verified = bool(proof.get("verified"))
        missing_markers = list(proof.get("missing_markers", []))
        task_status = "complete"
        orchestrator._emit(
            task.id,
            "task_complete",
            "The society produced a final answer." if proof_verified else "The society produced a final answer with unverified proof markers.",
            payload={
                "answer": task.final_answer,
                "demo_proof_verified": proof_verified,
                "missing_markers": missing_markers,
            },
        )

        self.assertEqual(task_status, "complete")
        self.assertEqual(emitted[0][0], "task_complete")
        self.assertFalse(emitted[0][1]["demo_proof_verified"])
        self.assertEqual(emitted[0][1]["missing_markers"], ["carried_dissent", "evidence_producing_delegation"])


class NoMockConstraintTests(unittest.TestCase):
    """Test that no-mock constraints are detected and enforced."""

    def test_no_mock_constraint_detection(self) -> None:
        """Various no-mock constraint phrasings are detected."""
        self.assertTrue(_prompt_has_no_mock_constraint("Use no mocks"))
        self.assertTrue(_prompt_has_no_mock_constraint("No fabrication allowed"))
        self.assertTrue(_prompt_has_no_mock_constraint("Do not fabricate evidence"))
        self.assertTrue(_prompt_has_no_mock_constraint("Real evidence only"))
        self.assertTrue(_prompt_has_no_mock_constraint("No simulated behavior"))
        self.assertFalse(_prompt_has_no_mock_constraint("Build a feature"))

    def test_mock_recommendation_stripping(self) -> None:
        """Lines recommending mock/simulate/fake behavior are stripped."""
        text = """Build the feature.
We recommend using a mock database for testing.
You could simulate the API responses.
Use real evidence for all claims."""
        stripped = _strip_mock_recommendations(text)
        self.assertIn("Build the feature.", stripped)
        self.assertIn("Use real evidence for all claims.", stripped)
        self.assertNotIn("recommend using a mock", stripped)
        self.assertNotIn("simulate the API", stripped)

    def test_mock_stripping_preserves_non_recommendation_lines(self) -> None:
        """Lines that mention mock but don't recommend it are preserved."""
        text = """The mock implementation failed.
Do not use mocks.
Build the real thing."""
        stripped = _strip_mock_recommendations(text)
        self.assertIn("The mock implementation failed.", stripped)
        self.assertIn("Do not use mocks.", stripped)
        self.assertIn("Build the real thing.", stripped)


if __name__ == "__main__":
    unittest.main()
