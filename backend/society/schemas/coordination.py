from __future__ import annotations

from pydantic import BaseModel, Field


class CoordinationSubtaskHint(BaseModel):
    """One delegation-ready subtask hint from the team coordination pass."""

    agent_id: str = Field(description="Target agent id for this subtask")
    subtask: str = Field(description="Concrete subtask objective")
    why_assigned: str = Field(default="", description="Why this agent is the right owner")
    done_criteria: list[str] = Field(default_factory=list, description="Conditions for considering this subtask done")
    blocking_if_missing: bool = Field(default=False, description="Whether execution is blocked if this subtask cannot be completed")


class TeamCoordinationBrief(BaseModel):
    """Structured output from the Agno Team coordination pass.

    This is the primary truth artifact from the team's initial coordination.
    Fields are consumed directly by delegation, working brief construction,
    failure diagnostics, and observable backend truth.
    """

    summary: str = Field(description="Concise shared understanding of the task")
    proposed_subtasks: list[CoordinationSubtaskHint] = Field(
        default_factory=list,
        description="Delegation-ready subtask hints with agent assignments",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Unresolved questions that affect execution quality",
    )
    recommended_focus: str = Field(
        default="",
        description="Single most important focus area for execution",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Team confidence in the coordination brief",
    )
    risk_signals: list[str] = Field(
        default_factory=list,
        description="Things that could go wrong; consumed by monitoring and critique",
    )
    evidence_gaps: list[str] = Field(
        default_factory=list,
        description="Missing evidence the team identified; consumed by researcher subtasks",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Explicit assumptions the team is making; carried into caveats",
    )
    delegation_hints: list[str] = Field(
        default_factory=list,
        description="Free-text delegation guidance beyond structured subtask hints",
    )
