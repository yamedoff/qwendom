from __future__ import annotations

from pydantic import BaseModel, Field


class MetricRecord(BaseModel):
    """Acknowledgement from record_metric tool."""

    status: str = "recorded"
    metric_name: str = ""
    value: float = 0.0
    context: str = ""
    created_at: str = ""


class TaskMetrics(BaseModel):
    """Structured metrics emitted after each completed task."""

    task_id: str
    total_duration_seconds: float = 0.0
    governance_rounds: int = 0
    debate_rounds: int = 0
    tool_calls_total: int = 0
    tool_calls_failed: int = 0
    proposals_count: int = 0
    challenges_count: int = 0
    revisions_count: int = 0
    vote_margin: float = 0.0
    critique_risk_count: int = 0
    child_agents_spawned: int = 0
    memory_writes: int = 0
    answer_length: int = 0
    evaluation_metrics: list[dict] = Field(default_factory=list)


class GovernanceMetrics(BaseModel):
    """Governance quality indicators for a single task."""

    leader_election_confidence: float = 0.0
    average_vote_confidence: float = 0.0
    debate_productivity: float = 0.0
    reputation_variance: float = 0.0


class MemoryMetrics(BaseModel):
    """Memory and knowledge utilization metrics for a single task."""

    memories_created: int = 0
    memories_retrieved: int = 0
    knowledge_searches: int = 0
    session_summary_generated: bool = False
