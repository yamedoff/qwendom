"""Bounded dependency-aware execution for validated team composition plans.

This module runs a static ``TeamCompositionPlan`` without invoking provider
tools directly. It enforces runtime concurrency limits, defensive conflict
locks, typed retry behavior, additive durable-compatible events, and
single-use execution semantics.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .error_taxonomy import classify_error, compute_backoff, is_retryable
from .schemas.team_composition import (
    CompositionLimits,
    TeamAssignment,
    TeamCompositionPlan,
    TeamCompositionEventType,
    WorkNode,
    _paths_overlap,
)

EventSink = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]
NodeRunner = Callable[[WorkNode, TeamAssignment, int, asyncio.Event], Awaitable[Mapping[str, Any] | None]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]
CancellationWaitFn = Callable[
    [set[asyncio.Task[Any]], float],
    Awaitable[tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]],
]

_MAX_ERROR_MESSAGE_LENGTH = 500
_MAX_RESULT_KEYS = 32
_MAX_RESULT_ITEMS = 32
_MAX_RESULT_DEPTH = 3
_MAX_STRING_LENGTH = 500
_MAX_ARTIFACT_REFS = 16


class WorkNodeStatus(StrEnum):
    """Stable per-node runtime status values."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELED = "canceled"


class WorkGraphTerminalStatus(StrEnum):
    """Stable aggregate execution terminal states."""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELED = "canceled"


class RetrySettings(BaseModel):
    """Injectable retry policy for work-node execution attempts."""

    max_attempts: int = Field(default=3, ge=1)
    backoff_base_seconds: float = Field(default=1.0, ge=0.0)
    backoff_cap_seconds: float = Field(default=30.0, ge=0.0)
    jitter: bool = False


class NodeExecutionError(RuntimeError):
    """Explicit typed failure raised by a node runner."""

    def __init__(self, category: str, code: str, message: str) -> None:
        self.category = category
        self.code = code
        self.message = message
        super().__init__(message)


class WorkNodeAttemptRecord(BaseModel):
    """One bounded execution attempt for a work node."""

    attempt: int
    status: WorkNodeStatus
    category: str | None = None
    code: str | None = None
    message: str | None = None
    started_at: float
    finished_at: float
    duration_seconds: float


class WorkNodeRuntimeRecord(BaseModel):
    """Typed per-node execution state returned by the executor."""

    node_id: str
    assignment_id: str
    status: WorkNodeStatus = WorkNodeStatus.PENDING
    attempts: list[WorkNodeAttemptRecord] = Field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    blocked_dependency_ids: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)


class WorkGraphExecutionResult(BaseModel):
    """Aggregate executor output with bounded node-level runtime detail."""

    terminal_status: WorkGraphTerminalStatus
    started_at: float
    finished_at: float
    duration_seconds: float
    execution_order: list[str] = Field(default_factory=list)
    max_model_concurrency: int = 0
    max_agentbay_concurrency: int = 0
    max_media_concurrency: int = 0
    uncooperative_task_ids: list[str] = Field(default_factory=list)
    nodes: dict[str, WorkNodeRuntimeRecord] = Field(default_factory=dict)


@dataclass(slots=True)
class _AttemptOutcome:
    node_id: str
    attempt: int
    started_at: float
    finished_at: float
    status: WorkNodeStatus
    category: str | None = None
    code: str | None = None
    message: str | None = None
    result: dict[str, Any] | None = None


class BoundedWorkGraphExecutor:
    """Run a validated work graph under bounded runtime limits.

    The executor is intentionally single-use so internal state and emitted
    events always describe exactly one run. Python cannot forcibly terminate a
    coroutine that deliberately suppresses cancellation, so shutdown records
    any still-running nodes as uncooperative and returns without awaiting them
    forever.
    """

    def __init__(
        self,
        plan: TeamCompositionPlan,
        limits: CompositionLimits,
        node_runner: NodeRunner,
        event_sink: EventSink,
        *,
        sleep: SleepFn = asyncio.sleep,
        clock: ClockFn | None = None,
        retry_settings: RetrySettings | None = None,
        global_cancellation_event: asyncio.Event | None = None,
        cancellation_grace_seconds: float = 0.05,
        cancellation_wait: CancellationWaitFn | None = None,
    ) -> None:
        self._plan = plan
        self._limits = limits
        self._node_runner = node_runner
        self._event_sink = event_sink
        self._sleep = sleep
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._retry = retry_settings or RetrySettings()
        self._global_cancellation_event = global_cancellation_event
        self._cancellation_grace_seconds = max(0.0, cancellation_grace_seconds)
        self._cancellation_wait = cancellation_wait or self._default_cancellation_wait
        self._used = False

        self._assignments = {assignment.id: assignment for assignment in plan.assignments}
        self._nodes = {node.id: node for node in plan.work_graph}
        self._dependents: dict[str, set[str]] = {node_id: set() for node_id in self._nodes}
        for node in plan.work_graph:
            for dependency_id in node.depends_on:
                if dependency_id in self._dependents:
                    self._dependents[dependency_id].add(node.id)

        self._runtime: dict[str, WorkNodeRuntimeRecord] = {
            node.id: WorkNodeRuntimeRecord(node_id=node.id, assignment_id=node.assignment_id)
            for node in plan.work_graph
        }
        self._attempt_counts = {node.id: 0 for node in plan.work_graph}
        self._next_eligible_at = {node.id: 0.0 for node in plan.work_graph}
        self._running_tasks: dict[str, asyncio.Task[_AttemptOutcome]] = {}
        self._running_cancel_events: dict[str, asyncio.Event] = {}
        self._active_nodes: set[str] = set()
        self._execution_order: list[str] = []
        self._uncooperative_task_ids: set[str] = set()

        self._current_model_concurrency = 0
        self._current_agentbay_concurrency = 0
        self._current_media_concurrency = 0
        self._max_model_concurrency = 0
        self._max_agentbay_concurrency = 0
        self._max_media_concurrency = 0

    async def run(self) -> WorkGraphExecutionResult:
        """Execute the graph once and return typed runtime records."""

        if self._used:
            raise RuntimeError("BoundedWorkGraphExecutor instances may only be used for one run")
        self._used = True

        started_at = self._clock()
        if not self._plan.work_graph:
            finished_at = self._clock()
            return WorkGraphExecutionResult(
                terminal_status=WorkGraphTerminalStatus.COMPLETED,
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=finished_at - started_at,
                nodes={},
            )

        if self._is_globally_canceled():
            await self._cancel_pending_nodes()
            return self._build_result(WorkGraphTerminalStatus.CANCELED, started_at)

        while True:
            if self._is_globally_canceled():
                await self._cancel_all_active_work()
                return self._build_result(WorkGraphTerminalStatus.CANCELED, started_at)

            await self._propagate_dependency_blocks()
            if self._all_terminal():
                return self._build_result(self._terminal_status(), started_at)

            now = self._clock()
            ready = self._eligible_ready_nodes(now)
            if ready:
                selected = self._select_ready_subset(ready)
                if selected:
                    await self._launch_ready_subset(selected)

            if self._running_tasks:
                waited_tasks: set[asyncio.Task[Any]] = set(self._running_tasks.values())
                cancellation_wait_task: asyncio.Task[bool] | None = None
                if self._global_cancellation_event is not None:
                    cancellation_wait_task = asyncio.create_task(self._global_cancellation_event.wait())
                    waited_tasks.add(cancellation_wait_task)
                done, pending = await asyncio.wait(
                    waited_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                cancellation_triggered = (
                    cancellation_wait_task is not None and cancellation_wait_task in done
                )
                if cancellation_wait_task is not None and not cancellation_triggered:
                    cancellation_wait_task.cancel()
                    await asyncio.gather(cancellation_wait_task, return_exceptions=True)
                if cancellation_triggered:
                    done.remove(cancellation_wait_task)
                    pending.discard(cancellation_wait_task)
                if cancellation_wait_task is not None:
                    pending.discard(cancellation_wait_task)
                for task in done:
                    outcome = await task
                    await self._handle_attempt_outcome(outcome)
                if cancellation_triggered:
                    await self._cancel_all_active_work()
                    return self._build_result(WorkGraphTerminalStatus.CANCELED, started_at)
                continue

            if self._all_terminal():
                return self._build_result(self._terminal_status(), started_at)

            future_retry_at = self._next_retry_time()
            if future_retry_at is not None and future_retry_at > now:
                await self._sleep_until(future_retry_at)
                continue

            await self._mark_deadlocked_nodes()
            return self._build_result(self._terminal_status(), started_at)

    def _all_terminal(self) -> bool:
        return all(
            record.status in {
                WorkNodeStatus.COMPLETED,
                WorkNodeStatus.FAILED,
                WorkNodeStatus.BLOCKED,
                WorkNodeStatus.CANCELED,
            }
            for record in self._runtime.values()
        )

    def _terminal_status(self) -> WorkGraphTerminalStatus:
        statuses = {record.status for record in self._runtime.values()}
        if WorkNodeStatus.CANCELED in statuses:
            return WorkGraphTerminalStatus.CANCELED
        if WorkNodeStatus.FAILED in statuses:
            return WorkGraphTerminalStatus.FAILED
        if WorkNodeStatus.BLOCKED in statuses:
            return WorkGraphTerminalStatus.BLOCKED
        return WorkGraphTerminalStatus.COMPLETED

    def _eligible_ready_nodes(self, now: float) -> list[WorkNode]:
        ready: list[WorkNode] = []
        for node in sorted(self._plan.work_graph, key=lambda item: item.id):
            record = self._runtime[node.id]
            if record.status != WorkNodeStatus.PENDING:
                continue
            if self._next_eligible_at[node.id] > now:
                continue
            if any(dependency_id not in self._runtime for dependency_id in node.depends_on):
                continue
            if all(
                self._runtime[dependency_id].status == WorkNodeStatus.COMPLETED
                for dependency_id in node.depends_on
            ):
                ready.append(node)
        return ready

    def _select_ready_subset(self, ready: list[WorkNode]) -> list[WorkNode]:
        selected: list[WorkNode] = []
        projected_model = self._current_model_concurrency
        projected_agentbay = self._current_agentbay_concurrency
        projected_media = self._current_media_concurrency
        for node in sorted(ready, key=lambda item: item.id):
            assignment = self._assignments.get(node.assignment_id)
            if assignment is None:
                continue
            next_model = projected_model + 1
            next_agentbay = projected_agentbay + int(self._requires_agentbay(assignment))
            next_media = projected_media + int(self._requires_media(assignment))
            if next_model > self._limits.max_model_workers:
                continue
            if next_agentbay > self._limits.max_agentbay_sessions:
                continue
            if next_media > self._limits.max_media_jobs:
                continue
            if self._conflicts_with_active_work(node, assignment):
                continue
            if any(
                self._nodes_conflict(node, assignment, selected_node, self._assignments[selected_node.assignment_id])
                for selected_node in selected
            ):
                continue
            selected.append(node)
            projected_model = next_model
            projected_agentbay = next_agentbay
            projected_media = next_media
        return selected

    async def _launch_ready_subset(self, selected: list[WorkNode]) -> None:
        for node in selected:
            assignment = self._assignments.get(node.assignment_id)
            if assignment is None:
                continue
            attempt = self._attempt_counts[node.id] + 1
            cancel_event = asyncio.Event()
            self._running_cancel_events[node.id] = cancel_event
            self._reserve_resources(node.id, assignment)
            record = self._runtime[node.id]
            record.status = WorkNodeStatus.RUNNING
            if record.started_at is None:
                record.started_at = self._clock()
            self._attempt_counts[node.id] = attempt
            if node.id not in self._execution_order:
                self._execution_order.append(node.id)
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_STARTED.value,
                {
                    "node_id": node.id,
                    "assignment_id": assignment.id,
                    "attempt": attempt,
                    "status": WorkNodeStatus.RUNNING.value,
                    "started_at": self._clock(),
                },
            )
            self._running_tasks[node.id] = asyncio.create_task(
                self._run_attempt(node, assignment, attempt, cancel_event)
            )

    async def _run_attempt(
        self,
        node: WorkNode,
        assignment: TeamAssignment,
        attempt: int,
        cancellation_event: asyncio.Event,
    ) -> _AttemptOutcome:
        started_at = self._clock()
        try:
            result = await self._node_runner(node, assignment, attempt, cancellation_event)
            return _AttemptOutcome(
                node_id=node.id,
                attempt=attempt,
                started_at=started_at,
                finished_at=self._clock(),
                status=WorkNodeStatus.COMPLETED,
                result=_bounded_mapping(result or {}),
            )
        except asyncio.CancelledError:
            return _AttemptOutcome(
                node_id=node.id,
                attempt=attempt,
                started_at=started_at,
                finished_at=self._clock(),
                status=WorkNodeStatus.CANCELED,
                category="transient",
                code="canceled",
                message="canceled",
            )
        except NodeExecutionError as exc:
            return _AttemptOutcome(
                node_id=node.id,
                attempt=attempt,
                started_at=started_at,
                finished_at=self._clock(),
                status=WorkNodeStatus.FAILED,
                category=exc.category,
                code=exc.code,
                message=_bounded_text(exc.message),
            )
        except Exception as exc:  # pragma: no cover - exercised in tests via classification.
            category = classify_error(exc)
            return _AttemptOutcome(
                node_id=node.id,
                attempt=attempt,
                started_at=started_at,
                finished_at=self._clock(),
                status=WorkNodeStatus.FAILED,
                category=category,
                code=type(exc).__name__,
                message=_bounded_text(str(exc) or type(exc).__name__),
            )
        finally:
            self._running_cancel_events.pop(node.id, None)
            self._running_tasks.pop(node.id, None)
            self._release_resources(node.id, assignment)

    async def _handle_attempt_outcome(self, outcome: _AttemptOutcome) -> None:
        node = self._nodes[outcome.node_id]
        assignment = self._assignments[node.assignment_id]
        record = self._runtime[outcome.node_id]
        record.attempts.append(
            WorkNodeAttemptRecord(
                attempt=outcome.attempt,
                status=outcome.status,
                category=outcome.category,
                code=outcome.code,
                message=outcome.message,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                duration_seconds=outcome.finished_at - outcome.started_at,
            )
        )

        if outcome.status == WorkNodeStatus.COMPLETED:
            record.status = WorkNodeStatus.COMPLETED
            record.finished_at = outcome.finished_at
            record.result = outcome.result or {}
            record.artifact_refs = _extract_artifact_refs(record.result)
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_COMPLETED.value,
                {
                    "node_id": node.id,
                    "assignment_id": assignment.id,
                    "attempt": outcome.attempt,
                    "status": WorkNodeStatus.COMPLETED.value,
                    "started_at": outcome.started_at,
                    "finished_at": outcome.finished_at,
                    "duration_seconds": outcome.finished_at - outcome.started_at,
                },
            )
            return

        if outcome.status == WorkNodeStatus.CANCELED or self._is_globally_canceled():
            record.status = WorkNodeStatus.CANCELED
            record.finished_at = outcome.finished_at
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_CANCELED.value,
                {
                    "node_id": node.id,
                    "assignment_id": assignment.id,
                    "attempt": outcome.attempt,
                    "status": WorkNodeStatus.CANCELED.value,
                    "category": outcome.category,
                    "started_at": outcome.started_at,
                    "finished_at": outcome.finished_at,
                    "duration_seconds": outcome.finished_at - outcome.started_at,
                },
            )
            return

        category = outcome.category or "transient"
        attempts_exhausted = self._attempt_counts[node.id] >= self._retry.max_attempts
        if is_retryable(category) and not attempts_exhausted:
            backoff = compute_backoff(
                self._attempt_counts[node.id],
                base=self._retry.backoff_base_seconds,
                cap=self._retry.backoff_cap_seconds,
                jitter=self._retry.jitter,
            )
            record.status = WorkNodeStatus.PENDING
            self._next_eligible_at[node.id] = outcome.finished_at + backoff
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_RETRY_SCHEDULED.value,
                {
                    "node_id": node.id,
                    "assignment_id": assignment.id,
                    "attempt": outcome.attempt,
                    "status": WorkNodeStatus.PENDING.value,
                    "category": category,
                    "started_at": outcome.started_at,
                    "finished_at": outcome.finished_at,
                    "duration_seconds": outcome.finished_at - outcome.started_at,
                    "backoff_seconds": backoff,
                },
            )
            return

        record.status = WorkNodeStatus.FAILED
        record.finished_at = outcome.finished_at
        await self._emit_event(
            TeamCompositionEventType.WORK_NODE_FAILED.value,
            {
                "node_id": node.id,
                "assignment_id": assignment.id,
                "attempt": outcome.attempt,
                "status": WorkNodeStatus.FAILED.value,
                "category": category,
                "started_at": outcome.started_at,
                "finished_at": outcome.finished_at,
                "duration_seconds": outcome.finished_at - outcome.started_at,
            },
        )

    async def _propagate_dependency_blocks(self) -> None:
        changed = True
        while changed:
            changed = False
            for node in sorted(self._plan.work_graph, key=lambda item: item.id):
                record = self._runtime[node.id]
                if record.status != WorkNodeStatus.PENDING:
                    continue
                blocked_dependencies = [
                    dependency_id
                    for dependency_id in node.depends_on
                    if dependency_id not in self._runtime
                    or self._runtime[dependency_id].status in {
                        WorkNodeStatus.FAILED,
                        WorkNodeStatus.BLOCKED,
                        WorkNodeStatus.CANCELED,
                    }
                ]
                if not blocked_dependencies:
                    continue
                record.status = WorkNodeStatus.BLOCKED
                record.finished_at = self._clock()
                record.blocked_dependency_ids = sorted(blocked_dependencies)
                await self._emit_event(
                    TeamCompositionEventType.WORK_NODE_BLOCKED.value,
                    {
                        "node_id": node.id,
                        "assignment_id": record.assignment_id,
                        "attempt": self._attempt_counts[node.id],
                        "status": WorkNodeStatus.BLOCKED.value,
                        "category": "dependency_blocked",
                        "finished_at": record.finished_at,
                    },
                )
                changed = True

    async def _mark_deadlocked_nodes(self) -> None:
        now = self._clock()
        for node in sorted(self._plan.work_graph, key=lambda item: item.id):
            record = self._runtime[node.id]
            if record.status != WorkNodeStatus.PENDING:
                continue
            record.status = WorkNodeStatus.BLOCKED
            record.finished_at = now
            record.blocked_dependency_ids = sorted(
                dependency_id
                for dependency_id in node.depends_on
                if dependency_id not in self._runtime
                or self._runtime[dependency_id].status != WorkNodeStatus.COMPLETED
            )
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_BLOCKED.value,
                {
                    "node_id": node.id,
                    "assignment_id": record.assignment_id,
                    "attempt": self._attempt_counts[node.id],
                    "status": WorkNodeStatus.BLOCKED.value,
                    "category": "deadlock",
                    "finished_at": now,
                },
            )

    async def _cancel_pending_nodes(self) -> None:
        now = self._clock()
        for node in sorted(self._plan.work_graph, key=lambda item: item.id):
            record = self._runtime[node.id]
            if record.status != WorkNodeStatus.PENDING:
                continue
            record.status = WorkNodeStatus.CANCELED
            record.finished_at = now
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_CANCELED.value,
                {
                    "node_id": node.id,
                    "assignment_id": record.assignment_id,
                    "attempt": self._attempt_counts[node.id],
                    "status": WorkNodeStatus.CANCELED.value,
                    "finished_at": now,
                },
            )

    async def _cancel_all_active_work(self) -> None:
        now = self._clock()
        done_tasks: set[asyncio.Task[_AttemptOutcome]] = set()
        pending_tasks = set(self._running_tasks.values())
        for _ in range(2):
            if not pending_tasks:
                break
            for node_id, task in list(self._running_tasks.items()):
                if task not in pending_tasks:
                    continue
                cancel_event = self._running_cancel_events.get(node_id)
                if cancel_event is not None:
                    cancel_event.set()
                task.cancel()
            completed, still_pending = await self._cancellation_wait(
                pending_tasks,
                self._cancellation_grace_seconds,
            )
            done_tasks.update(completed)
            pending_tasks = still_pending

        for task in done_tasks:
            if task.cancelled():
                continue
            outcome = await task
            await self._handle_attempt_outcome(outcome)

        for node_id, task in list(self._running_tasks.items()):
            if task not in pending_tasks:
                continue
            self._detach_uncooperative_task(node_id)

        for node in sorted(self._plan.work_graph, key=lambda item: item.id):
            record = self._runtime[node.id]
            if record.status in {
                WorkNodeStatus.COMPLETED,
                WorkNodeStatus.FAILED,
                WorkNodeStatus.BLOCKED,
                WorkNodeStatus.CANCELED,
            }:
                continue
            record.status = WorkNodeStatus.CANCELED
            record.finished_at = now
            await self._emit_event(
                TeamCompositionEventType.WORK_NODE_CANCELED.value,
                {
                    "node_id": node.id,
                    "assignment_id": record.assignment_id,
                    "attempt": self._attempt_counts[node.id],
                    "status": WorkNodeStatus.CANCELED.value,
                    "finished_at": now,
                },
            )

    def _next_retry_time(self) -> float | None:
        waiting_times = [
            retry_at
            for node_id, retry_at in self._next_eligible_at.items()
            if self._runtime[node_id].status == WorkNodeStatus.PENDING and retry_at > self._clock()
        ]
        return min(waiting_times) if waiting_times else None

    async def _sleep_until(self, target_time: float) -> None:
        delay = max(0.0, target_time - self._clock())
        if delay <= 0.0:
            return
        if self._global_cancellation_event is None:
            await self._sleep(delay)
            return
        sleep_task = asyncio.create_task(self._sleep(delay))
        cancel_task = asyncio.create_task(self._global_cancellation_event.wait())
        done, pending = await asyncio.wait(
            {sleep_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done:
            sleep_task.cancel()
            await asyncio.gather(sleep_task, return_exceptions=True)

    def _reserve_resources(self, node_id: str, assignment: TeamAssignment) -> None:
        self._active_nodes.add(node_id)
        self._current_model_concurrency += 1
        if self._requires_agentbay(assignment):
            self._current_agentbay_concurrency += 1
        if self._requires_media(assignment):
            self._current_media_concurrency += 1
        self._max_model_concurrency = max(self._max_model_concurrency, self._current_model_concurrency)
        self._max_agentbay_concurrency = max(self._max_agentbay_concurrency, self._current_agentbay_concurrency)
        self._max_media_concurrency = max(self._max_media_concurrency, self._current_media_concurrency)

    def _release_resources(self, node_id: str, assignment: TeamAssignment) -> None:
        if node_id not in self._active_nodes:
            return
        self._active_nodes.remove(node_id)
        self._current_model_concurrency = max(0, self._current_model_concurrency - 1)
        if self._requires_agentbay(assignment):
            self._current_agentbay_concurrency = max(0, self._current_agentbay_concurrency - 1)
        if self._requires_media(assignment):
            self._current_media_concurrency = max(0, self._current_media_concurrency - 1)

    def _conflicts_with_active_work(self, node: WorkNode, assignment: TeamAssignment) -> bool:
        for active_id in self._active_nodes:
            active_node = self._nodes[active_id]
            active_assignment = self._assignments.get(active_node.assignment_id)
            if active_assignment is None:
                continue
            if self._nodes_conflict(node, assignment, active_node, active_assignment):
                return True
        return False

    def _nodes_conflict(
        self,
        left_node: WorkNode,
        left_assignment: TeamAssignment,
        right_node: WorkNode,
        right_assignment: TeamAssignment,
    ) -> bool:
        for left_path in left_assignment.owned_paths:
            for right_path in right_assignment.owned_paths:
                if _paths_overlap(left_path, right_path):
                    return True
        return bool(
            _normalized_domains(left_node.conflict_domains)
            .intersection(_normalized_domains(right_node.conflict_domains))
        )

    def _requires_agentbay(self, assignment: TeamAssignment) -> bool:
        return "sandbox_execution" in assignment.required_capabilities

    def _requires_media(self, assignment: TeamAssignment) -> bool:
        return bool({"image_generation", "video_generation"}.intersection(assignment.required_capabilities))

    def _is_globally_canceled(self) -> bool:
        return self._global_cancellation_event is not None and self._global_cancellation_event.is_set()

    async def _emit_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        result = self._event_sink(event_type, dict(payload))
        if inspect.isawaitable(result):
            await result

    async def _default_cancellation_wait(
        self,
        tasks: set[asyncio.Task[Any]],
        timeout: float,
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        if not tasks:
            return set(), set()
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        return set(done), set(pending)

    def _detach_uncooperative_task(self, node_id: str) -> None:
        node = self._nodes[node_id]
        assignment = self._assignments[node.assignment_id]
        self._uncooperative_task_ids.add(node_id)
        task = self._running_tasks.pop(node_id, None)
        self._running_cancel_events.pop(node_id, None)
        self._release_resources(node_id, assignment)
        if task is not None:
            task.add_done_callback(self._consume_detached_task_outcome)

    def _consume_detached_task_outcome(self, task: asyncio.Task[_AttemptOutcome]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    def _build_result(
        self,
        terminal_status: WorkGraphTerminalStatus,
        started_at: float,
    ) -> WorkGraphExecutionResult:
        finished_at = self._clock()
        return WorkGraphExecutionResult(
            terminal_status=terminal_status,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=finished_at - started_at,
            execution_order=list(self._execution_order),
            max_model_concurrency=self._max_model_concurrency,
            max_agentbay_concurrency=self._max_agentbay_concurrency,
            max_media_concurrency=self._max_media_concurrency,
            uncooperative_task_ids=sorted(self._uncooperative_task_ids),
            nodes={node_id: record.model_copy(deep=True) for node_id, record in self._runtime.items()},
        )


def _bounded_text(value: Any) -> str:
    text = str(value)
    return text[:_MAX_ERROR_MESSAGE_LENGTH]


def _bounded_mapping(value: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key in list(value.keys())[:_MAX_RESULT_KEYS]:
        bounded[str(key)[:_MAX_STRING_LENGTH]] = _bounded_value(value[key], depth + 1)
    return bounded


def _bounded_value(value: Any, depth: int) -> Any:
    if depth > _MAX_RESULT_DEPTH:
        return _bounded_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STRING_LENGTH]
    if isinstance(value, Mapping):
        return _bounded_mapping(value, depth)
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth + 1) for item in list(value)[:_MAX_RESULT_ITEMS]]
    return _bounded_text(value)


def _extract_artifact_refs(result: Mapping[str, Any]) -> list[str]:
    raw_refs = result.get("artifact_refs")
    if not isinstance(raw_refs, list):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for item in raw_refs[:_MAX_ARTIFACT_REFS]:
        ref = str(item)[:_MAX_STRING_LENGTH]
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def _normalized_domains(domains: list[str]) -> set[str]:
    return {domain.strip().casefold() for domain in domains if domain.strip()}
