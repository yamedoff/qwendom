"""Phase 5: Generic acceptance evidence and terminal-state correctness.

Tests that:
- _populate_acceptance_checks produces a generic acceptance contract with
  stable requirement ids, required flags, evidence types, evidence event ids,
  passed status, and missing evidence.
- acceptance_evidence_evaluated is emitted with the full evaluation payload.
- _complete_after_vote routes to the correct terminal state:
  - complete: all required checks pass
  - complete_with_warnings: required pass, optional failures
  - remediation: required failures with recoverable subtask work
  - failed: required failures with no recovery
- demo_proof_verified is supplementary, not a universal gate.
- _derive_final_status handles all new event types.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from society.models import SocietyEvent, TaskRun


def _event(event_type: str, payload: dict | None = None, task_id: str = "t1") -> SocietyEvent:
    return SocietyEvent(task_id=task_id, type=event_type, message="", payload=payload or {})


class AcceptanceEvidenceContractTests(unittest.TestCase):
    """_populate_acceptance_checks produces a generic contract."""

    def _make_orchestrator(self, state: dict, events: list[SocietyEvent] | None = None) -> object:
        from society.orchestrator import SocietyOrchestrator

        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch._state = lambda _task_id: state
        orch._safe_list_events = lambda _task_id: events or []
        orch._emit = lambda _task_id, etype, _msg, **kw: emitted.append({"type": etype, "payload": kw.get("payload", {})})
        return orch

    def setUp(self) -> None:
        self.emitted: list[dict] = []
        global emitted
        emitted = self.emitted

    def test_requirement_structure(self) -> None:
        state = {
            "proposals": {"p1": "Proposal 1"},
            "winner_id": "agent-1",
            "critique": {"critique": "Looks good"},
            "final_deliverable": {"answer": "Done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "Build something",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        evidence = state["acceptance_evidence"]
        self.assertIn("requirements", evidence)
        self.assertIn("terminal_status", evidence)

        for req in evidence["requirements"]:
            self.assertIn("id", req)
            self.assertIn("required", req)
            self.assertIn("evidence_types", req)
            self.assertIn("evidence_event_ids", req)
            self.assertIn("passed", req)
            self.assertIn("missing_evidence", req)
            self.assertIsInstance(req["required"], bool)
            self.assertIsInstance(req["passed"], bool)
            self.assertIsInstance(req["evidence_types"], list)
            self.assertIsInstance(req["evidence_event_ids"], list)
            self.assertIsInstance(req["missing_evidence"], list)

    def test_all_required_pass_yields_complete(self) -> None:
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        self.assertEqual(state["acceptance_evidence"]["terminal_status"], "complete")
        self.assertTrue(state["acceptance_evidence"]["required_passed"])
        self.assertEqual(state["acceptance_evidence"]["required_failures"], [])
        self.assertEqual(state["acceptance_evidence"]["optional_failures"], [])

    def test_fixed_composition_validation_and_cleanup_satisfy_final_acceptance_without_spawning_validator(self) -> None:
        state = {
            "proposals": {"p1": "x"}, "winner_id": "a1", "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}], "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []}, "prompt": "Repair calc.py",
            "fixed_specialist_selection": {"assignments": [
                {"assignment_id": "build", "template_id": "builder"},
                {"assignment_id": "validate", "template_id": "test_engineer"},
            ]},
        }
        events = [
            _event("local_independent_validation_reported", {"assignment_id": "validate", "passed": True}),
            _event("composition_assignment_cleanup_completed", {"assignment_id": "build", "success": True, "results": [{"success": True, "closed": True}]}),
            _event("composition_assignment_cleanup_completed", {"assignment_id": "validate", "success": True, "results": [{"success": True, "closed": True}]}),
        ]
        orch = self._make_orchestrator(state, events)
        orch._ensure_independent_validator = lambda *_args: self.fail("must not spawn a redundant validator")
        orch._populate_acceptance_checks("t1", SimpleNamespace(leader_id="leader"))
        asyncio.run(orch._run_independent_validator(TaskRun(prompt="Repair calc.py"), SimpleNamespace()))

        evidence = state["acceptance_evidence"]
        requirements = {item["id"]: item for item in evidence["requirements"]}
        self.assertEqual(evidence["terminal_status"], "complete")
        self.assertTrue(requirements["fixed_specialist_independent_validation"]["passed"])
        self.assertTrue(requirements["fixed_specialist_sandbox_cleanup"]["passed"])
        self.assertEqual(state["independent_validation"]["source"], "fixed_test_engineer_local_validation")

    def test_verified_fixed_composition_replaces_contradictory_prevalidation_answer(self) -> None:
        state = {
            "proposals": {"p1": "x"}, "winner_id": "a1", "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "Blocker/Missing Critical Validation Evidence"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}], "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []}, "prompt": "Repair calc.py",
            "fixed_specialist_selection": {"assignments": [
                {"assignment_id": "build", "template_id": "builder"},
                {"assignment_id": "validate", "template_id": "test_engineer"},
            ]},
        }
        events = [
            _event("local_independent_validation_reported", {"assignment_id": "validate", "passed": True}),
            _event("agentbay_artifact_exported", {"assignment_id": "build", "path": "src/calc.py"}),
            _event("composition_assignment_cleanup_completed", {"assignment_id": "build", "success": True, "results": [{"success": True, "closed": True}]}),
            _event("composition_assignment_cleanup_completed", {"assignment_id": "validate", "success": True, "results": [{"success": True, "closed": True}]}),
        ]
        orch = self._make_orchestrator(state, events)
        orch._populate_acceptance_checks("t1", SimpleNamespace(leader_id="leader"))
        task = TaskRun(prompt="Repair calc.py", final_answer="Blocker/Missing Critical Validation Evidence")

        answer = orch._apply_validation_gate(task, SimpleNamespace(leader_id="leader", member_ids=["leader"]))

        self.assertTrue(answer.startswith("Verified runtime outcome:"))
        self.assertIn("src/calc.py", answer)
        self.assertNotIn("Blocker/Missing", answer)
        self.assertEqual(state["final_deliverable"]["validation_status"], "verified_fixed_composition")

    def test_failed_fixed_checks_do_not_replace_the_prevalidation_answer(self) -> None:
        state = {
            "acceptance_evidence": {"required_passed": False, "requirements": [
                {"id": "fixed_specialist_independent_validation", "passed": False},
                {"id": "fixed_specialist_sandbox_cleanup", "passed": True},
            ]},
            "acceptance_checks": [{"check": "fixed_specialist_independent_validation", "passed": False}],
            "failed_checks": [{"check": "fixed_specialist_independent_validation", "reason": "validation failed"}],
            "final_deliverable": {"answer": "Blocker/Missing Critical Validation Evidence"},
        }
        orch = self._make_orchestrator(state)
        orch._register_artifact = lambda *args, **kwargs: None
        task = TaskRun(prompt="Repair calc.py", final_answer="Blocker/Missing Critical Validation Evidence")

        answer = orch._apply_validation_gate(task, SimpleNamespace(leader_id="leader", member_ids=["leader"]))

        self.assertIn("Blocker/Missing Critical Validation Evidence", answer)
        self.assertNotIn("Verified runtime outcome:", answer)

    def test_optional_gap_does_not_preserve_a_contradictory_fixed_runtime_answer(self) -> None:
        state = {
            "acceptance_evidence": {
                "required_passed": True,
                "required_failures": [],
                "optional_failures": ["demo_proof_verified"],
                "requirements": [
                    {"id": "fixed_specialist_independent_validation", "passed": True},
                    {"id": "fixed_specialist_sandbox_cleanup", "passed": True},
                ],
            },
            "acceptance_checks": [{"check": "demo_proof_verified", "passed": False}],
            "failed_checks": [{"check": "demo_proof_verified", "reason": "optional markers"}],
            "final_deliverable": {"answer": "Artifact evidence is missing"},
        }
        orch = self._make_orchestrator(state, [
            _event("agentbay_artifact_exported", {"workspace_relative_path": "app/dist/index.html"}),
        ])
        task = TaskRun(prompt="Build page", final_answer="Artifact evidence is missing")

        answer = orch._apply_validation_gate(task, SimpleNamespace(leader_id="leader", member_ids=["leader"]))

        self.assertTrue(answer.startswith("Verified runtime outcome:"))
        self.assertIn("Optional demo-proof gaps remain", answer)
        self.assertNotIn("Artifact evidence is missing", answer)

    def test_fixed_composition_failed_validation_and_missing_cleanup_remain_required_failures(self) -> None:
        state = {
            "proposals": {"p1": "x"}, "winner_id": "a1", "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}], "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []}, "prompt": "Repair calc.py",
            "fixed_specialist_selection": {"assignments": [
                {"assignment_id": "build", "template_id": "builder"},
                {"assignment_id": "validate", "template_id": "test_engineer"},
            ]},
        }
        orch = self._make_orchestrator(state, [
            _event("local_independent_validation_reported", {"assignment_id": "validate", "passed": False}),
            _event("composition_assignment_cleanup_completed", {"assignment_id": "build", "success": False, "results": [{"success": False, "closed": False}]}),
            _event("composition_assignment_cleanup_completed", {"assignment_id": "validate", "success": True, "results": [{"success": True, "closed": True}]}),
        ])
        orch._populate_acceptance_checks("t1", SimpleNamespace(leader_id="leader"))

        self.assertEqual(state["acceptance_evidence"]["terminal_status"], "failed")
        self.assertIn("fixed_specialist_independent_validation", state["acceptance_evidence"]["required_failures"])
        self.assertIn("fixed_specialist_sandbox_cleanup", state["acceptance_evidence"]["required_failures"])

    def test_optional_failure_yields_complete_with_warnings(self) -> None:
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": None,
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        self.assertEqual(state["acceptance_evidence"]["terminal_status"], "complete_with_warnings")
        self.assertTrue(state["acceptance_evidence"]["required_passed"])
        self.assertIn("critique_exists", state["acceptance_evidence"]["optional_failures"])

    def test_blocking_subtask_failure_is_required(self) -> None:
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [{
                "id": "s1", "status": "failed", "blockers": ["missing output"],
                "blocking_if_missing": True, "exhausted": True,
            }],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        orch._populate_acceptance_checks("t1", SimpleNamespace(leader_id="leader"))

        evidence = state["acceptance_evidence"]
        self.assertIn("no_blocked_subtasks", evidence["required_failures"])
        self.assertEqual(evidence["terminal_status"], "failed")

    def test_required_failure_with_recoverable_subtasks_yields_remediation(self) -> None:
        state = {
            "proposals": {},
            "winner_id": "a1",
            "critique": None,
            "final_deliverable": None,
            "artifacts": [],
            "subtasks": [{"id": "s1", "status": "failed", "blockers": [], "exhausted": False}],
            "demo_proof": {"verified": False, "missing_markers": ["x"]},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        self.assertEqual(state["acceptance_evidence"]["terminal_status"], "remediation")
        self.assertFalse(state["acceptance_evidence"]["required_passed"])
        self.assertTrue(state["acceptance_evidence"]["recoverable"])

    def test_required_failure_exhausted_yields_failed(self) -> None:
        state = {
            "proposals": {},
            "winner_id": None,
            "critique": None,
            "final_deliverable": None,
            "artifacts": [],
            "subtasks": [{"id": "s1", "status": "failed", "blockers": [], "exhausted": True}],
            "demo_proof": {"verified": False, "missing_markers": ["x"]},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        self.assertEqual(state["acceptance_evidence"]["terminal_status"], "failed")
        self.assertFalse(state["acceptance_evidence"]["recoverable"])

    def test_acceptance_evidence_evaluated_emitted(self) -> None:
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        eval_events = [e for e in self.emitted if e["type"] == "acceptance_evidence_evaluated"]
        self.assertEqual(len(eval_events), 1)
        payload = eval_events[0]["payload"]
        self.assertIn("requirements", payload)
        self.assertIn("terminal_status", payload)
        self.assertIn("required_passed", payload)
        self.assertIn("required_failures", payload)
        self.assertIn("optional_failures", payload)
        self.assertIn("recoverable", payload)

    def test_evidence_event_ids_populated_from_task_events(self) -> None:
        events = [
            _event("agent_proposal_submitted", task_id="t1"),
            _event("winner_selected", task_id="t1"),
            _event("peer_monitor_report", task_id="t1"),
        ]
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state, events)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        evidence = state["acceptance_evidence"]
        proposals_req = next(r for r in evidence["requirements"] if r["id"] == "proposals_exist")
        self.assertTrue(len(proposals_req["evidence_event_ids"]) > 0)

    def test_demo_proof_not_required(self) -> None:
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": {"critique": "ok"},
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": False, "missing_markers": ["carried_dissent"]},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        evidence = state["acceptance_evidence"]
        demo_req = next(r for r in evidence["requirements"] if r["id"] == "demo_proof_verified")
        self.assertFalse(demo_req["required"])
        self.assertEqual(evidence["terminal_status"], "complete_with_warnings")

    def test_legacy_acceptance_checks_still_populated(self) -> None:
        state = {
            "proposals": {"p1": "x"},
            "winner_id": "a1",
            "critique": None,
            "final_deliverable": {"answer": "done"},
            "artifacts": [{"type": "final_deliverable", "status": "final"}],
            "subtasks": [],
            "demo_proof": {"verified": True, "missing_markers": []},
            "prompt": "test",
        }
        orch = self._make_orchestrator(state)
        team = SimpleNamespace(leader_id="leader")
        orch._populate_acceptance_checks("t1", team)

        self.assertIsInstance(state["acceptance_checks"], list)
        self.assertIsInstance(state["failed_checks"], list)
        self.assertTrue(len(state["acceptance_checks"]) > 0)


class DeriveFinalStatusTests(unittest.TestCase):
    """_derive_final_status handles all terminal event types."""

    def test_complete(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [_event("task_complete", {"acceptance_status": "complete"})]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "complete")

    def test_complete_with_warnings(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [_event("task_complete", {"acceptance_status": "complete_with_warnings"})]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "complete_with_warnings")

    def test_complete_default_when_no_acceptance_status(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [_event("task_complete", {})]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "complete")

    def test_remediation(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [_event("task_remediation", {})]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "remediation")

    def test_failed(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [_event("task_failed", {})]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "failed")

    def test_complete_supersedes_later_failed(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [
            _event("task_complete", {"acceptance_status": "complete"}),
            _event("task_failed", {}),
        ]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "complete")

    def test_complete_with_warnings_supersedes_later_failed(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [
            _event("task_complete", {"acceptance_status": "complete_with_warnings"}),
            _event("task_failed", {}),
        ]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "complete_with_warnings")

    def test_waiting_for_user(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [_event("user_clarification_requested", {})]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "waiting_for_user")

    def test_resume_after_waiting(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        events = [
            _event("user_clarification_requested", {}),
            _event("society_resumed", {}),
        ]
        self.assertEqual(SocietyOrchestrator._derive_final_status(events), "running")


class ProjectionsStatusTests(unittest.TestCase):
    """Projection _task_status handles new event types."""

    def test_complete_with_warnings(self) -> None:
        from society.projections import _task_status

        events = [_event("task_complete", {"acceptance_status": "complete_with_warnings"})]
        self.assertEqual(_task_status(events), "complete_with_warnings")

    def test_remediation(self) -> None:
        from society.projections import _task_status

        events = [_event("task_remediation", {})]
        self.assertEqual(_task_status(events), "remediation")

    def test_completion_outcome_complete_with_warnings(self) -> None:
        from society.projections import _completion_outcome

        events = [_event("task_complete", {"acceptance_status": "complete_with_warnings"})]
        self.assertEqual(_completion_outcome(events), "complete_with_warnings")

    def test_completion_outcome_remediation(self) -> None:
        from society.projections import _completion_outcome

        events = [_event("task_remediation", {})]
        self.assertEqual(_completion_outcome(events), "remediation")

    def test_phase_acceptance_evaluation(self) -> None:
        from society.projections import _phase

        events = [_event("acceptance_evidence_evaluated", {})]
        self.assertEqual(_phase(events), "acceptance_evaluation")

    def test_phase_remediation(self) -> None:
        from society.projections import _phase

        events = [_event("task_remediation", {})]
        self.assertEqual(_phase(events), "remediation")


class MemoryStatusTests(unittest.TestCase):
    """EventStore status derivation handles new events."""

    def test_task_summary_complete_with_warnings(self) -> None:
        from society.memory import EventStore

        store = EventStore.__new__(EventStore)
        events = [
            _event("task_received", {"prompt": "test"}, task_id="t1"),
            _event("task_complete", {"acceptance_status": "complete_with_warnings", "answer": "done"}, task_id="t1"),
        ]
        store._events = events
        store._task_order = ["t1"]
        store._task_index = {"t1": [0, 1]}
        import threading
        store._lock = threading.Lock()

        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "complete_with_warnings")

    def test_task_summaries_remediation(self) -> None:
        from society.memory import EventStore

        store = EventStore.__new__(EventStore)
        store._events = [
            _event("task_received", {"prompt": "test"}, task_id="t1"),
            _event("task_remediation", {}, task_id="t1"),
        ]
        store._task_order = ["t1"]
        import threading
        store._lock = threading.Lock()

        summaries = store.task_summaries()
        self.assertEqual(summaries[0]["status"], "remediation")


class TaskRunModelTests(unittest.TestCase):
    """TaskRun accepts new status values."""

    def test_complete_with_warnings_status(self) -> None:
        task = TaskRun(prompt="test", status="complete_with_warnings")
        self.assertEqual(task.status, "complete_with_warnings")

    def test_remediation_status(self) -> None:
        task = TaskRun(prompt="test", status="remediation")
        self.assertEqual(task.status, "remediation")


class CompleteAfterVoteTerminalStateTests(unittest.TestCase):
    """_complete_after_vote routes to correct terminal states."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_orchestrator(self, terminal_status: str, required_failures=None, optional_failures=None):
        from society.orchestrator import SocietyOrchestrator

        state = {
            "demo_proof": {"verified": True, "missing_markers": []},
            "acceptance_evidence": {
                "terminal_status": terminal_status,
                "required_failures": required_failures or [],
                "optional_failures": optional_failures or [],
                "recoverable": terminal_status == "remediation",
                "required_passed": terminal_status in {"complete", "complete_with_warnings"},
            },
            "phase": "validation",
            "current_actor": None,
        }
        emitted = []
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch._state = lambda _task_id: state
        orch._emit = lambda _tid, etype, msg, **kw: emitted.append({"type": etype, "payload": kw.get("payload", {}), "message": msg})
        orch._record_task_metrics = lambda *a, **kw: None
        orch._diagnose_failure = lambda _tid: {"missing_inputs": [], "missing_evidence": []}
        orch._monitor = AsyncMock()
        orch._compose_answer = lambda task, team, winner, proposals: "Final answer"
        orch._record_demo_proof = lambda task, team: None
        orch._populate_acceptance_checks = lambda tid, team: None
        orch._apply_validation_gate = lambda task, team: "Final answer"
        orch._record_evaluation_metrics = AsyncMock()
        orch._learn = AsyncMock()
        orch._dissolve = lambda task, team: None
        return orch, emitted, state

    def test_complete_emits_task_complete(self) -> None:
        orch, emitted, state = self._make_orchestrator("complete")
        task = TaskRun(prompt="test")
        task.final_answer = "Final answer"
        team = SimpleNamespace(leader_id="leader")

        self._run(orch._complete_after_vote(task, team, "w", {}, 0.0))

        self.assertEqual(task.status, "complete")
        complete_events = [e for e in emitted if e["type"] == "task_complete"]
        self.assertEqual(len(complete_events), 1)
        self.assertEqual(complete_events[0]["payload"]["acceptance_status"], "complete")

    def test_complete_with_warnings_emits_task_complete_with_warnings(self) -> None:
        orch, emitted, state = self._make_orchestrator("complete_with_warnings", optional_failures=["critique_exists"])
        task = TaskRun(prompt="test")
        task.final_answer = "Final answer"
        team = SimpleNamespace(leader_id="leader")

        self._run(orch._complete_after_vote(task, team, "w", {}, 0.0))

        self.assertEqual(task.status, "complete_with_warnings")
        complete_events = [e for e in emitted if e["type"] == "task_complete"]
        self.assertEqual(len(complete_events), 1)
        self.assertEqual(complete_events[0]["payload"]["acceptance_status"], "complete_with_warnings")
        self.assertIn("critique_exists", complete_events[0]["payload"]["optional_failures"])

    def test_remediation_emits_task_remediation(self) -> None:
        orch, emitted, state = self._make_orchestrator("remediation", required_failures=["proposals_exist"])
        task = TaskRun(prompt="test")
        task.final_answer = "Final answer"
        team = SimpleNamespace(leader_id="leader")

        self._run(orch._complete_after_vote(task, team, "w", {}, 0.0))

        self.assertEqual(task.status, "remediation")
        rem_events = [e for e in emitted if e["type"] == "task_remediation"]
        self.assertEqual(len(rem_events), 1)
        self.assertIn("proposals_exist", rem_events[0]["payload"]["required_failures"])
        self.assertTrue(rem_events[0]["payload"]["recoverable"])

    def test_failed_emits_run_failed_and_task_failed(self) -> None:
        orch, emitted, state = self._make_orchestrator("failed", required_failures=["winner_selected", "final_deliverable_exists"])
        task = TaskRun(prompt="test")
        task.final_answer = "Final answer"
        team = SimpleNamespace(leader_id="leader")

        self._run(orch._complete_after_vote(task, team, "w", {}, 0.0))

        self.assertEqual(task.status, "failed")
        run_failed = [e for e in emitted if e["type"] == "run_failed"]
        task_failed = [e for e in emitted if e["type"] == "task_failed"]
        self.assertEqual(len(run_failed), 1)
        self.assertEqual(len(task_failed), 1)
        self.assertFalse(run_failed[0]["payload"]["recoverable"])
        self.assertIn("winner_selected", task_failed[0]["payload"]["required_failures"])

    def test_no_task_complete_on_remediation(self) -> None:
        orch, emitted, state = self._make_orchestrator("remediation", required_failures=["proposals_exist"])
        task = TaskRun(prompt="test")
        task.final_answer = "Final answer"
        team = SimpleNamespace(leader_id="leader")

        self._run(orch._complete_after_vote(task, team, "w", {}, 0.0))

        complete_events = [e for e in emitted if e["type"] == "task_complete"]
        self.assertEqual(len(complete_events), 0)

    def test_no_task_complete_on_failed(self) -> None:
        orch, emitted, state = self._make_orchestrator("failed", required_failures=["proposals_exist"])
        task = TaskRun(prompt="test")
        task.final_answer = "Final answer"
        team = SimpleNamespace(leader_id="leader")

        self._run(orch._complete_after_vote(task, team, "w", {}, 0.0))

        complete_events = [e for e in emitted if e["type"] == "task_complete"]
        self.assertEqual(len(complete_events), 0)


if __name__ == "__main__":
    unittest.main()
