from __future__ import annotations

import json

from pydantic import BaseModel, Field, field_validator


def _normalize_text_list(value: object) -> list[str]:
    """Coerce common model-generated list variants into a stable string list."""

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return []
        if cleaned.startswith("["):
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except json.JSONDecodeError:
                pass
        return [cleaned]
    return [str(value)]


class CoordinationSubtaskHint(BaseModel):
    """One delegation-ready subtask hint from the team coordination pass."""

    agent_id: str = Field(description="Target agent id for this subtask")
    subtask: str = Field(description="Concrete subtask objective")
    why_assigned: str = Field(default="", description="Why this agent is the right owner")
    done_criteria: list[str] = Field(default_factory=list, description="Conditions for considering this subtask done")
    blocking_if_missing: bool = Field(default=False, description="Whether execution is blocked if this subtask cannot be completed")

    @field_validator("done_criteria", mode="before")
    @classmethod
    def normalize_done_criteria(cls, value: object) -> list[str]:
        """Accept a single prose criterion without rejecting the whole brief."""

        return _normalize_text_list(value)

    @field_validator("blocking_if_missing", mode="before")
    @classmethod
    def normalize_blocking_flag(cls, value: object) -> bool:
        """Use conservative false for prose that is not an explicit boolean."""

        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        return False


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

    @field_validator(
        "open_questions",
        "risk_signals",
        "evidence_gaps",
        "assumptions",
        "delegation_hints",
        mode="before",
    )
    @classmethod
    def normalize_text_lists(cls, value: object) -> list[str]:
        """Keep useful prose when a model supplies one string instead of a list."""

        return _normalize_text_list(value)
