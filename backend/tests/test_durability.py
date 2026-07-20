from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.memory import EventStore
from society.models import AgentProfile, SocietyAgent, SocietyEvent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.governance import VoteDecision


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "cerebras",
        "CEREBRAS_API_KEY": "test-key",
        "CEREBRAS_MODEL": "test-model",
        "LLM_TIMEOUT_SECONDS": 10,
        "READINESS_CONCURRENCY": 3,
        "EFFICIENT_SOCIETY_ENABLED": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_orchestrator(settings: Settings | None = None, event_store: EventStore | None = None) -> SocietyOrchestrator:
    settings = settings or _make_settings()
    with patch("society.orchestrator.get_agno_db", return_value=None):
        with patch("society.orchestrator.get_settings", return_value=settings):
            orch = SocietyOrchestrator(settings=settings)
    if event_store is not None:
        orch.events = event_store
        orch._restore_from_events()
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


def _make_task_and_team(orch: SocietyOrchestrator, roster: list[str]) -> tuple[TaskRun, Team]:
    task = TaskRun(prompt="Test task for durability", status="running")
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


class VoteResolutionRestartTests(unittest.IsolatedAsyncioTestCase):
    """Simulate a backend restart while a task is paused at vote_resolution.

    The first orchestrator pauses a task at vote_resolution, emitting events
    to a real JSONL-backed EventStore. A second orchestrator is then created
    with the same event store, simulating a restart. The test verifies that
    the second orchestrator reconstructs the task and can accept a
    clarification answer to resume post-vote.
    """

    async def _pause_at_vote_resolution(self, orch: SocietyOrchestrator) -> tuple[str, list[str]]:
        roster = ["architect", "researcher", "builder"]
        orch.agents = _make_agents(roster)
        task, team = _make_task_and_team(orch, roster)
        team.leader_id = "architect"

        proposals = {aid: f"full-proposal-text-for-{aid}" for aid in roster}

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
                result = await orch._vote(task, team, proposals)

        self.assertIsNone(result)
        self.assertEqual(task.status, "waiting_for_user")
        return task.id, roster

    async def test_restart_reconstructs_vote_resolution_task(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()

        orch1 = _make_orchestrator(settings, store)
        task_id, roster = await self._pause_at_vote_resolution(orch1)

        orch2 = _make_orchestrator(settings, store)

        self.assertIn(task_id, orch2.tasks)
        restored_task = orch2.tasks[task_id]
        self.assertEqual(restored_task.status, "waiting_for_user")
        self.assertEqual(restored_task.prompt, "Test task for durability")
        persisted_events = store.list(task_id)
        self.assertEqual(restored_task.created_at, persisted_events[0].created_at)
        self.assertEqual(restored_task.updated_at, persisted_events[-1].created_at)

        self.assertTrue(len(orch2.teams) > 0)
        restored_team = orch2.teams.get(restored_task.team_id)
        self.assertIsNotNone(restored_team)
        self.assertEqual(set(restored_team.member_ids), set(roster))
        self.assertEqual(restored_team.leader_id, "architect")

        state = orch2.session_states.get(task_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["resume_phase"], "vote_resolution")
        vote_res = state["vote_resolution"]
        self.assertEqual(set(vote_res["candidates"]), set(roster))
        for cid in roster:
            self.assertIn(cid, vote_res["proposals"])
            self.assertEqual(vote_res["proposals"][cid], f"full-proposal-text-for-{cid}")

    async def test_restart_accepts_clarification_and_resumes(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()

        orch1 = _make_orchestrator(settings, store)
        task_id, roster = await self._pause_at_vote_resolution(orch1)

        orch2 = _make_orchestrator(settings, store)

        result_task = orch2.apply_user_clarification(task_id, "builder")
        self.assertEqual(result_task.status, "running")
        state = orch2.session_states[task_id]
        self.assertEqual(state["winner_id"], "builder")

        orch2._monitor = AsyncMock()
        orch2._record_evaluation_metrics = AsyncMock()
        orch2._learn = AsyncMock()
        orch2._dissolve = lambda t, tm: None
        orch2._compose_answer = lambda t, tm, w, p: f"answer by {w}"
        orch2._populate_acceptance_checks = lambda tid, tm: None
        orch2._apply_validation_gate = lambda t, tm: "validated"
        orch2._record_demo_proof = lambda t, tm: None
        orch2._record_task_metrics = lambda t, st: None

        await orch2.continue_after_clarification(task_id)

        orch2._monitor.assert_called_once()
        self.assertEqual(orch2.tasks[task_id].status, "complete")

    async def test_restart_invalid_answer_still_returns_409(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()

        orch1 = _make_orchestrator(settings, store)
        task_id, roster = await self._pause_at_vote_resolution(orch1)

        orch2 = _make_orchestrator(settings, store)

        with self.assertRaises(ValueError) as ctx:
            orch2.apply_user_clarification(task_id, "nonexistent_agent")
        self.assertIn("not a valid candidate", str(ctx.exception))
        self.assertEqual(orch2.tasks[task_id].status, "waiting_for_user")

    async def test_restart_does_not_rerun_pre_vote_phases(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()

        orch1 = _make_orchestrator(settings, store)
        task_id, roster = await self._pause_at_vote_resolution(orch1)

        orch2 = _make_orchestrator(settings, store)
        orch2.apply_user_clarification(task_id, "architect")

        call_log: list[str] = []
        for name in ["_run_agno_team", "_elect_leader", "_spawn_child_agent", "_negotiate", "_delegate_subtasks", "_collect_proposal_opinions"]:
            def _make_tracker(n):
                async def _tracker(*a, **kw):
                    call_log.append(n)
                return _tracker
            setattr(orch2, name, _make_tracker(name))
        orch2._monitor = AsyncMock()
        orch2._record_evaluation_metrics = AsyncMock()
        orch2._learn = AsyncMock()
        orch2._dissolve = lambda t, tm: None
        orch2._compose_answer = lambda t, tm, w, p: "answer"
        orch2._populate_acceptance_checks = lambda tid, tm: None
        orch2._apply_validation_gate = lambda t, tm: "ok"
        orch2._record_demo_proof = lambda t, tm: None
        orch2._record_task_metrics = lambda t, st: None

        await orch2.continue_after_clarification(task_id)

        for name in ["_run_agno_team", "_elect_leader", "_spawn_child_agent", "_negotiate", "_delegate_subtasks", "_collect_proposal_opinions"]:
            self.assertNotIn(name, call_log, f"{name} should not be called during vote_resolution resume after restart")


class ReadinessClarificationRestartTests(unittest.TestCase):
    """Readiness pauses must remain actionable after backend restart."""

    def test_restart_reconstructs_and_accepts_readiness_clarification(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        task_id = "readiness-paused-task"
        roster = ["architect", "researcher", "builder", "critic"]
        store.append(SocietyEvent(
            task_id=task_id,
            type="task_received",
            message="received",
            payload={"prompt": "Compare two launch approaches with visible evidence."},
        ))
        store.append(SocietyEvent(
            task_id=task_id,
            type="team_formed",
            message="formed",
            payload={
                "team": {
                    "id": "restored-team",
                    "task_id": task_id,
                    "member_ids": roster,
                    "voter_ids": roster,
                    "leader_id": None,
                    "status": "active",
                }
            },
        ))
        store.append(SocietyEvent(
            task_id=task_id,
            type="user_clarification_requested",
            message="clarify",
            payload={
                "question": "What counts as visible evidence?",
                "blockers": ["Evidence definition is missing."],
                "open_questions": [],
                "resume_phase": "post_readiness",
            },
        ))

        orch = _make_orchestrator(_make_settings(), store)

        self.assertIn(task_id, orch.tasks)
        self.assertEqual(orch.tasks[task_id].status, "waiting_for_user")
        self.assertEqual(orch.tasks[task_id].team_id, "restored-team")
        self.assertIn("restored-team", orch.teams)
        self.assertEqual(orch._state(task_id)["resume_phase"], "post_readiness")
        persisted_events = store.list(task_id)
        self.assertEqual(orch.tasks[task_id].created_at, persisted_events[0].created_at)
        self.assertEqual(orch.tasks[task_id].updated_at, persisted_events[-1].created_at)

        resumed = orch.apply_user_clarification(task_id, "Use the structured risk matrix as evidence.")

        self.assertEqual(resumed.status, "running")
        self.assertEqual(orch._state(task_id)["user_clarification"]["status"], "answered")
        self.assertTrue(orch._state(task_id)["ready_to_proceed"])


class StartupInterruptionReconciliationTests(unittest.TestCase):
    """A new process must not present orphaned work as still running."""

    def test_startup_marks_only_stale_running_tasks_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = Path(temp_dir) / "events.jsonl"
            store = EventStore(event_path)
            store.append(SocietyEvent(task_id="running", type="task_received", message="received", payload={"prompt": "run"}))
            store.append(SocietyEvent(task_id="complete", type="task_received", message="received", payload={"prompt": "done"}))
            store.append(SocietyEvent(task_id="complete", type="task_complete", message="done", payload={}))
            store.append(SocietyEvent(task_id="waiting", type="task_received", message="received", payload={"prompt": "wait"}))
            store.append(SocietyEvent(task_id="waiting", type="user_clarification_requested", message="wait", payload={}))

            orchestrator = _make_orchestrator(_make_settings(EVENT_STORE_FILE=str(event_path)))

            self.assertEqual(orchestrator.get_task_summary("running")["status"], "interrupted")
            self.assertEqual(orchestrator.get_task_summary("complete")["status"], "complete")
            self.assertEqual(orchestrator.get_task_summary("waiting")["status"], "waiting_for_user")
            interruptions = [event for event in orchestrator.list_events("running") if event.type == "task_interrupted"]
            self.assertEqual(len(interruptions), 1)
            self.assertEqual(interruptions[0].payload["reason"], "process_restart")

            restarted = _make_orchestrator(_make_settings(EVENT_STORE_FILE=str(event_path)))
            repeated = [event for event in restarted.list_events("running") if event.type == "task_interrupted"]
            self.assertEqual(len(repeated), 1)


class GracefulShutdownInterruptionTests(unittest.IsolatedAsyncioTestCase):
    """Verify that graceful shutdown marks running tasks as interrupted."""

    def test_shutdown_marks_running_tasks_as_interrupted(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)

        task1 = TaskRun(prompt="task one", status="running")
        task2 = TaskRun(prompt="task two", status="running")
        orch.tasks[task1.id] = task1
        orch.tasks[task2.id] = task2
        orch._state(task1.id)["phase"] = "negotiating"
        orch._state(task2.id)["phase"] = "voting"

        orch.shutdown()

        self.assertEqual(task1.status, "interrupted")
        self.assertEqual(task2.status, "interrupted")

        events1 = store.list(task1.id)
        types1 = [e.type for e in events1]
        self.assertIn("task_interrupted", types1)
        self.assertIn("task_failed", types1)

        interrupt_event = next(e for e in events1 if e.type == "task_interrupted")
        self.assertEqual(interrupt_event.payload["phase"], "negotiating")
        self.assertEqual(interrupt_event.payload["reason"], "graceful_shutdown")

    def test_shutdown_does_not_affect_completed_tasks(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)

        task = TaskRun(prompt="done task", status="complete")
        orch.tasks[task.id] = task
        orch._emit(task.id, "task_received", "received", payload={"prompt": "done task"})
        orch._emit(task.id, "task_complete", "done", payload={"answer": "yes"})

        orch.shutdown()

        self.assertEqual(task.status, "complete")
        events = store.list(task.id)
        interrupt_events = [e for e in events if e.type == "task_interrupted"]
        self.assertEqual(len(interrupt_events), 0)

    def test_shutdown_does_not_affect_waiting_for_user_tasks(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)

        task = TaskRun(prompt="paused task", status="waiting_for_user")
        orch.tasks[task.id] = task
        orch._emit(task.id, "task_received", "received", payload={"prompt": "paused task"})
        orch._emit(task.id, "user_clarification_requested", "paused", payload={"resume_phase": "vote_resolution"})

        orch.shutdown()

        self.assertEqual(task.status, "waiting_for_user")
        events = store.list(task.id)
        interrupt_events = [e for e in events if e.type == "task_interrupted"]
        self.assertEqual(len(interrupt_events), 0)

    def test_shutdown_events_make_task_summary_report_interrupted(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)

        task = TaskRun(prompt="interrupted task", status="running")
        orch.tasks[task.id] = task
        orch._emit(task.id, "task_received", "received", payload={"prompt": "interrupted task"})
        orch._state(task.id)["phase"] = "executing"

        orch.shutdown()

        summary = store.task_summary(task.id)
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(orch.get_task_summary(task.id)["status"], "interrupted")
        listed = next(item for item in orch.list_task_summaries() if item["id"] == task.id)
        self.assertEqual(listed["status"], "interrupted")

    def test_shutdown_emits_interrupted_before_failed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)

        task = TaskRun(prompt="ordering task", status="running")
        orch.tasks[task.id] = task
        orch._emit(task.id, "task_received", "received", payload={"prompt": "ordering task"})

        orch.shutdown()

        events = store.list(task.id)
        types = [e.type for e in events]
        interrupted_idx = types.index("task_interrupted")
        failed_idx = types.index("task_failed")
        self.assertLess(interrupted_idx, failed_idx)


class InterruptedStatusInSummariesTests(unittest.TestCase):
    def test_task_summary_reports_interrupted(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        store.append(SocietyEvent(task_id="t1", type="task_received", message="r", payload={"prompt": "p"}))
        store.append(SocietyEvent(task_id="t1", type="task_interrupted", message="i", payload={"phase": "voting", "reason": "graceful_shutdown"}))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "interrupted")

    def test_task_summaries_reports_interrupted(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        store.append(SocietyEvent(task_id="t1", type="task_received", message="r", payload={"prompt": "p"}))
        store.append(SocietyEvent(task_id="t1", type="task_interrupted", message="i", payload={"phase": "voting"}))
        summaries = store.task_summaries()
        self.assertEqual(summaries[0]["status"], "interrupted")

    def test_complete_overrides_interrupted(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        store.append(SocietyEvent(task_id="t1", type="task_received", message="r", payload={"prompt": "p"}))
        store.append(SocietyEvent(task_id="t1", type="task_interrupted", message="i", payload={}))
        store.append(SocietyEvent(task_id="t1", type="task_complete", message="c", payload={"answer": "done"}))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "complete")

    def test_failure_after_interruption_is_the_terminal_summary_status(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        store.append(SocietyEvent(task_id="t1", type="task_received", message="r", payload={"prompt": "p"}))
        store.append(SocietyEvent(task_id="t1", type="task_interrupted", message="i", payload={}))
        store.append(SocietyEvent(task_id="t1", type="task_failed", message="f", payload={"error": "shutdown"}))
        self.assertEqual(store.task_summary("t1")["status"], "failed")


class MainShutdownHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_handler_cancels_background_tasks(self) -> None:
        import main
        original_tasks = set(main._background_tasks)
        original_shutdown = main.society.shutdown

        cancel_called = []

        class FakeTask:
            def __init__(self):
                self._cancelled = False
            def cancel(self):
                self._cancelled = True
                cancel_called.append(self)
            def __await__(self):
                async def _await():
                    raise asyncio.CancelledError()
                return _await().__await__()

        fake_task = FakeTask()
        main._background_tasks.add(fake_task)

        shutdown_called = []
        def fake_shutdown():
            shutdown_called.append(True)
        main.society.shutdown = fake_shutdown

        try:
            await main._on_shutdown()
            self.assertTrue(fake_task._cancelled)
            self.assertEqual(len(shutdown_called), 1)
        finally:
            main._background_tasks.clear()
            main._background_tasks.update(original_tasks)
            main.society.shutdown = original_shutdown


if __name__ == "__main__":
    unittest.main()
