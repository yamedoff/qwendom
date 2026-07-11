from __future__ import annotations

from threading import Lock

from .schemas.evaluation import TaskMetrics


class MetricsCollector:
    """Thread-safe collector for task evaluation metrics.

    Stores TaskMetrics snapshots from completed governance runs and
    provides aggregate summaries for the GET /metrics endpoint.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: list[TaskMetrics] = []

    def record(self, metrics: TaskMetrics) -> None:
        with self._lock:
            self._records.append(metrics)

    def all(self) -> list[TaskMetrics]:
        with self._lock:
            return list(self._records)

    def summary(self) -> dict:
        """Return aggregate statistics across all recorded task metrics."""

        with self._lock:
            records = list(self._records)
        return summarize_task_metrics(records)


def summarize_task_metrics(records: list[TaskMetrics]) -> dict:
    """Return aggregate statistics for any in-memory or replayed metrics."""

    if not records:
        return {
            "task_count": 0,
            "avg_duration_seconds": 0.0,
            "avg_governance_rounds": 0.0,
            "avg_debate_rounds": 0.0,
            "total_tool_calls": 0,
            "total_tool_calls_failed": 0,
            "avg_proposals_count": 0.0,
            "avg_vote_margin": 0.0,
            "total_child_agents_spawned": 0,
            "total_memory_writes": 0,
        }

    count = len(records)
    return {
        "task_count": count,
        "avg_duration_seconds": round(sum(r.total_duration_seconds for r in records) / count, 3),
        "avg_governance_rounds": round(sum(r.governance_rounds for r in records) / count, 2),
        "avg_debate_rounds": round(sum(r.debate_rounds for r in records) / count, 2),
        "total_tool_calls": sum(r.tool_calls_total for r in records),
        "total_tool_calls_failed": sum(r.tool_calls_failed for r in records),
        "avg_proposals_count": round(sum(r.proposals_count for r in records) / count, 2),
        "avg_vote_margin": round(sum(r.vote_margin for r in records) / count, 3),
        "total_child_agents_spawned": sum(r.child_agents_spawned for r in records),
        "total_memory_writes": sum(r.memory_writes for r in records),
    }
