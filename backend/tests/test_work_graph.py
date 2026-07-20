"""Deterministic tests for the Phase 4 bounded work-graph executor."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.schemas.team_composition import (
    CompositionLimits,
    TeamAssignment,
    TeamCompositionEventType,
    TeamCompositionPlan,
    ToolGrant,
    WorkNode,
)
from society.work_graph import (
    BoundedWorkGraphExecutor,
    NodeExecutionError,
    RetrySettings,
    WorkGraphTerminalStatus,
    WorkNodeStatus,
)


def _assignment(
    assignment_id: str,
    template_id: str,
    *,
    required_capabilities: list[str] | None = None,
    owned_paths: list[str] | None = None,
) -> TeamAssignment:
    capabilities = required_capabilities or []
    return TeamAssignment(
        id=assignment_id,
        agent_template_id=template_id,
        objective=f"Run {assignment_id}",
        required_capabilities=capabilities,
        tool_grants=[ToolGrant(capability=capabilities[0], tool_ids=["x"])] if capabilities else [],
        owned_paths=owned_paths or [],
    )


def _plan(assignments: list[TeamAssignment], nodes: list[WorkNode]) -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Test work graph",
        assignments=assignments,
        work_graph=nodes,
        selection_rationale="Deterministic test plan.",
    )


class WorkGraphExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def _event_sink(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    def _fake_clock(self) -> tuple[callable, callable]:
        state = {"now": 0.0}

        def clock() -> float:
            return state["now"]

        async def sleep(delay: float) -> None:
            state["now"] += delay

        return clock, sleep

    async def test_independent_nodes_overlap_and_dependencies_wait(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation"]),
                _assignment("b", "builder", required_capabilities=["implementation"]),
                _assignment("c", "critic", required_capabilities=["validation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a"),
                WorkNode(id="b", assignment_id="b"),
                WorkNode(id="c", assignment_id="c", depends_on=["a"]),
            ],
        )
        running = 0
        peak_running = 0
        lock = asyncio.Lock()
        overlap = asyncio.Event()
        release_ab = asyncio.Event()
        a_finished = asyncio.Event()
        c_started = asyncio.Event()
        started: list[str] = []

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            nonlocal running, peak_running
            async with lock:
                started.append(node.id)
                running += 1
                peak_running = max(peak_running, running)
                if running == 2:
                    overlap.set()
            if node.id in {"a", "b"}:
                await release_ab.wait()
            if node.id == "a":
                a_finished.set()
            if node.id == "c":
                c_started.set()
                self.assertTrue(a_finished.is_set())
            async with lock:
                running -= 1
            return {"artifact_refs": [f"{node.id}-artifact"]}

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=2, max_media_jobs=2),
            runner,
            self._event_sink,
        )
        task = asyncio.create_task(executor.run())
        await overlap.wait()
        self.assertFalse(c_started.is_set())
        release_ab.set()
        result = await task
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.COMPLETED)
        self.assertEqual(peak_running, 2)
        self.assertEqual(started[:2], ["a", "b"])
        self.assertEqual(result.nodes["c"].status, WorkNodeStatus.COMPLETED)

    async def test_model_agentbay_and_media_caps_hold(self) -> None:
        plan = _plan(
            [
                _assignment("builder-1", "builder", required_capabilities=["implementation", "sandbox_execution"]),
                _assignment("builder-2", "builder", required_capabilities=["implementation", "sandbox_execution"]),
                _assignment("image-1", "image_creator", required_capabilities=["image_generation"]),
                _assignment("image-2", "image_creator", required_capabilities=["image_generation"]),
                _assignment("critic-1", "critic", required_capabilities=["validation"]),
            ],
            [
                WorkNode(id="builder-1", assignment_id="builder-1"),
                WorkNode(id="builder-2", assignment_id="builder-2"),
                WorkNode(id="critic-1", assignment_id="critic-1"),
                WorkNode(id="image-1", assignment_id="image-1"),
                WorkNode(id="image-2", assignment_id="image-2"),
            ],
        )
        gate = asyncio.Event()
        current_model = 0
        current_agentbay = 0
        current_media = 0
        peak_model = 0
        peak_agentbay = 0
        peak_media = 0
        lock = asyncio.Lock()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            nonlocal current_model, current_agentbay, current_media, peak_model, peak_agentbay, peak_media
            async with lock:
                current_model += 1
                if "sandbox_execution" in assignment.required_capabilities:
                    current_agentbay += 1
                if "image_generation" in assignment.required_capabilities or "video_generation" in assignment.required_capabilities:
                    current_media += 1
                peak_model = max(peak_model, current_model)
                peak_agentbay = max(peak_agentbay, current_agentbay)
                peak_media = max(peak_media, current_media)
            await gate.wait()
            async with lock:
                current_model -= 1
                if "sandbox_execution" in assignment.required_capabilities:
                    current_agentbay -= 1
                if "image_generation" in assignment.required_capabilities or "video_generation" in assignment.required_capabilities:
                    current_media -= 1
            return {}

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
        )
        task = asyncio.create_task(executor.run())
        await asyncio.sleep(0)
        self.assertLessEqual(peak_model, 2)
        self.assertLessEqual(peak_agentbay, 1)
        self.assertLessEqual(peak_media, 1)
        gate.set()
        result = await task
        self.assertEqual(result.max_model_concurrency, 2)
        self.assertEqual(result.max_agentbay_concurrency, 1)
        self.assertEqual(result.max_media_concurrency, 1)

    async def test_conflicting_paths_and_domains_serialize_while_nonconflicting_work_overlaps(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation"], owned_paths=["backend/shared"]),
                _assignment("b", "builder", required_capabilities=["implementation"], owned_paths=["backend/shared/file.py"]),
                _assignment("c", "builder", required_capabilities=["implementation"]),
                _assignment("d", "builder", required_capabilities=["implementation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a"),
                WorkNode(id="b", assignment_id="b"),
                WorkNode(id="c", assignment_id="c", conflict_domains=["ui"]),
                WorkNode(id="d", assignment_id="d", conflict_domains=["UI"]),
            ],
        )
        release_first_batch = asyncio.Event()
        started: list[str] = []
        first_batch_seen = asyncio.Event()
        running: set[str] = set()
        lock = asyncio.Lock()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            async with lock:
                started.append(node.id)
                running.add(node.id)
                if len(running) == 2:
                    first_batch_seen.set()
            if node.id in {"a", "c"}:
                await release_first_batch.wait()
            async with lock:
                if node.id == "b":
                    self.assertNotIn("a", running)
                if node.id == "d":
                    self.assertNotIn("c", running)
                running.remove(node.id)
            return {}

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=2, max_media_jobs=2),
            runner,
            self._event_sink,
        )
        task = asyncio.create_task(executor.run())
        await first_batch_seen.wait()
        self.assertEqual(started[:2], ["a", "c"])
        release_first_batch.set()
        result = await task
        self.assertEqual(result.execution_order, ["a", "c", "b", "d"])

    async def test_wide_frontier_uses_stable_greedy_selection(self) -> None:
        node_count = 300
        assignments = [
            _assignment(f"node-{index:03d}", "builder", required_capabilities=["implementation"])
            for index in range(node_count)
        ]
        nodes = [WorkNode(id=assignment.id, assignment_id=assignment.id) for assignment in assignments]
        plan = _plan(assignments, nodes)
        first_batch_gate = asyncio.Event()
        release_all = asyncio.Event()
        started: list[str] = []
        lock = asyncio.Lock()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            async with lock:
                started.append(node.id)
                if len(started) == 3:
                    first_batch_gate.set()
            await release_all.wait()
            return {}

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=3, max_agentbay_sessions=3, max_media_jobs=3),
            runner,
            self._event_sink,
        )
        task = asyncio.create_task(executor.run())
        await asyncio.wait_for(first_batch_gate.wait(), timeout=0.5)
        self.assertEqual(started[:3], ["node-000", "node-001", "node-002"])
        release_all.set()
        result = await asyncio.wait_for(task, timeout=10.0)
        self.assertEqual(result.execution_order[:5], ["node-000", "node-001", "node-002", "node-003", "node-004"])
        self.assertEqual(result.max_model_concurrency, 3)

    async def test_failed_prerequisite_blocks_only_dependants(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation"]),
                _assignment("b", "critic", required_capabilities=["validation"]),
                _assignment("c", "builder", required_capabilities=["implementation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a"),
                WorkNode(id="b", assignment_id="b", depends_on=["a"]),
                WorkNode(id="c", assignment_id="c"),
            ],
        )

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            if node.id == "a":
                raise NodeExecutionError("deterministic", "failed_check", "deterministic failure")
            return {}

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=2, max_media_jobs=2),
            runner,
            self._event_sink,
        ).run()
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.FAILED)
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.FAILED)
        self.assertEqual(result.nodes["b"].status, WorkNodeStatus.BLOCKED)
        self.assertEqual(result.nodes["b"].blocked_dependency_ids, ["a"])
        self.assertEqual(result.nodes["c"].status, WorkNodeStatus.COMPLETED)

    async def test_event_timestamps_and_order_are_truthful_for_overlap_and_dependency_release(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation"]),
                _assignment("b", "builder", required_capabilities=["implementation"]),
                _assignment("c", "critic", required_capabilities=["validation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a"),
                WorkNode(id="b", assignment_id="b"),
                WorkNode(id="c", assignment_id="c", depends_on=["a"]),
            ],
        )

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            if node.id in {"a", "b"}:
                await asyncio.sleep(0.03)
            else:
                await asyncio.sleep(0.01)
            return {}

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=2, max_media_jobs=2),
            runner,
            self._event_sink,
        ).run()
        started = {payload["node_id"]: payload for event, payload in self.events if event == TeamCompositionEventType.WORK_NODE_STARTED.value}
        completed = {payload["node_id"]: payload for event, payload in self.events if event == TeamCompositionEventType.WORK_NODE_COMPLETED.value}
        self.assertLess(started["a"]["started_at"], completed["a"]["finished_at"])
        self.assertLess(started["b"]["started_at"], completed["b"]["finished_at"])
        self.assertLessEqual(started["a"]["started_at"], started["b"]["started_at"])
        self.assertLessEqual(started["b"]["started_at"], completed["a"]["finished_at"])
        self.assertGreaterEqual(started["c"]["started_at"], completed["a"]["finished_at"])
        self.assertEqual(result.nodes["c"].status, WorkNodeStatus.COMPLETED)

    async def test_transient_failure_retries_then_succeeds(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )
        attempts = 0
        clock, sleep = self._fake_clock()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise NodeExecutionError("transient", "timeout", "timed out")
            return {"artifact_refs": ["ok"]}

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            sleep=sleep,
            clock=clock,
            retry_settings=RetrySettings(max_attempts=3, backoff_base_seconds=0.5, backoff_cap_seconds=5.0),
        ).run()
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.COMPLETED)
        self.assertEqual(attempts, 2)
        self.assertEqual(clock(), 0.5)
        self.assertIn(TeamCompositionEventType.WORK_NODE_RETRY_SCHEDULED.value, [event for event, _ in self.events])

    async def test_nonretryable_failure_never_retries(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )
        attempts = 0

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            raise NodeExecutionError("deterministic", "bad_input", "deterministic")

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
        ).run()
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.FAILED)
        self.assertEqual(attempts, 1)

    async def test_retry_exhaustion_fails(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )
        attempts = 0

        clock, sleep = self._fake_clock()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            raise NodeExecutionError("transient", "timeout", "timed out")

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            sleep=sleep,
            clock=clock,
            retry_settings=RetrySettings(max_attempts=2, backoff_base_seconds=0.1, backoff_cap_seconds=1.0),
        ).run()
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.FAILED)
        self.assertEqual(attempts, 2)

    async def test_cancellation_before_start_marks_pending_canceled(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )
        cancellation = asyncio.Event()
        cancellation.set()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            self.fail("runner should not be called when already canceled")
            return {}

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            global_cancellation_event=cancellation,
        ).run()
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.CANCELED)
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.CANCELED)

    async def test_cancellation_during_work_cleans_up_tasks_and_resources(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation", "sandbox_execution"]),
                _assignment("b", "image_creator", required_capabilities=["image_generation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a"),
                WorkNode(id="b", assignment_id="b"),
            ],
        )
        cancellation = asyncio.Event()
        started = asyncio.Event()
        start_count = 0
        lock = asyncio.Lock()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            nonlocal start_count
            async with lock:
                start_count += 1
                if start_count == 2:
                    started.set()
            await cancellation_event.wait()
            raise asyncio.CancelledError()

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            global_cancellation_event=cancellation,
        )
        task = asyncio.create_task(executor.run())
        await started.wait()
        cancellation.set()
        result = await task
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.CANCELED)
        self.assertEqual(executor._running_tasks, {})
        self.assertEqual(executor._current_model_concurrency, 0)
        self.assertEqual(executor._current_agentbay_concurrency, 0)
        self.assertEqual(executor._current_media_concurrency, 0)
        self.assertIn(TeamCompositionEventType.WORK_NODE_CANCELED.value, [event for event, _ in self.events])

    async def test_cancellation_retries_once_for_runner_that_swallows_first_cancelled_error(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )
        cancellation = asyncio.Event()
        swallowed_first_cancel = asyncio.Event()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            first_cancel_swallowed = False
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if not first_cancel_swallowed:
                        first_cancel_swallowed = True
                        swallowed_first_cancel.set()
                        continue
                    raise

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            global_cancellation_event=cancellation,
            cancellation_grace_seconds=0.01,
        )
        task = asyncio.create_task(executor.run())
        await asyncio.sleep(0)
        cancellation.set()
        result = await task
        await swallowed_first_cancel.wait()
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.CANCELED)
        self.assertEqual(result.uncooperative_task_ids, [])
        self.assertEqual(executor._running_tasks, {})
        self.assertEqual(executor._current_model_concurrency, 0)

    async def test_uncooperative_runner_returns_boundedly_and_reports_node(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )
        cancellation = asyncio.Event()
        release_runner = asyncio.Event()
        runner_finished = asyncio.Event()

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            while True:
                try:
                    await release_runner.wait()
                    runner_finished.set()
                    return {}
                except asyncio.CancelledError:
                    continue

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            global_cancellation_event=cancellation,
            cancellation_grace_seconds=0.01,
        )
        task = asyncio.create_task(executor.run())
        await asyncio.sleep(0)
        cancellation.set()
        result = await asyncio.wait_for(task, timeout=0.5)
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.CANCELED)
        self.assertEqual(result.uncooperative_task_ids, ["a"])
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.CANCELED)
        self.assertEqual(executor._running_tasks, {})
        self.assertEqual(executor._current_model_concurrency, 0)
        release_runner.set()
        await asyncio.wait_for(runner_finished.wait(), timeout=0.5)
        await asyncio.sleep(0)

    async def test_resource_counters_release_after_runner_exception(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation", "sandbox_execution"]),
                _assignment("b", "critic", required_capabilities=["validation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a"),
                WorkNode(id="b", assignment_id="b"),
            ],
        )

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            if node.id == "a":
                raise RuntimeError("unexpected failure")
            return {}

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
            retry_settings=RetrySettings(max_attempts=1),
        )
        await executor.run()
        self.assertEqual(executor._current_model_concurrency, 0)
        self.assertEqual(executor._current_agentbay_concurrency, 0)
        self.assertEqual(executor._current_media_concurrency, 0)

    async def test_failed_nodes_emit_failed_event_not_blocked(self) -> None:
        plan = _plan(
            [_assignment("a", "builder", required_capabilities=["implementation"])],
            [WorkNode(id="a", assignment_id="a")],
        )

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            raise NodeExecutionError("deterministic", "bad_input", "deterministic")

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            runner,
            self._event_sink,
        ).run()
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.FAILED)
        self.assertIn(TeamCompositionEventType.WORK_NODE_FAILED.value, [event for event, _ in self.events])
        failed_events = [payload for event, payload in self.events if event == TeamCompositionEventType.WORK_NODE_FAILED.value]
        self.assertEqual(len(failed_events), 1)

    async def test_cycle_is_reported_truthfully_without_validation(self) -> None:
        plan = _plan(
            [
                _assignment("a", "builder", required_capabilities=["implementation"]),
                _assignment("b", "builder", required_capabilities=["implementation"]),
            ],
            [
                WorkNode(id="a", assignment_id="a", depends_on=["b"]),
                WorkNode(id="b", assignment_id="b", depends_on=["a"]),
            ],
        )

        async def runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            self.fail("deadlocked nodes should never start")
            return {}

        result = await BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=2, max_agentbay_sessions=2, max_media_jobs=2),
            runner,
            self._event_sink,
        ).run()
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.BLOCKED)
        self.assertEqual(result.nodes["a"].status, WorkNodeStatus.BLOCKED)
        self.assertEqual(result.nodes["a"].blocked_dependency_ids, ["b"])
        self.assertEqual(result.nodes["b"].blocked_dependency_ids, ["a"])

    async def test_empty_graph_completes_and_executor_reuse_rejects(self) -> None:
        executor = BoundedWorkGraphExecutor(
            _plan([], []),
            CompositionLimits(max_model_workers=1, max_agentbay_sessions=1, max_media_jobs=1),
            lambda node, assignment, attempt, cancellation_event: asyncio.sleep(0),
            self._event_sink,
        )
        result = await executor.run()
        self.assertEqual(result.terminal_status, WorkGraphTerminalStatus.COMPLETED)
        with self.assertRaises(RuntimeError):
            await executor.run()


if __name__ == "__main__":
    unittest.main()
