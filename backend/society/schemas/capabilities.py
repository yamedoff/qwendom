from __future__ import annotations

from pydantic import BaseModel, Field


class TaskDecomposition(BaseModel):
    """Structured breakdown created by the architect before proposing work."""

    objective: str
    steps: list[str] = Field(min_length=1)
    delegation_plan: dict[str, str] = Field(default_factory=dict)


class MemoryLookup(BaseModel):
    """Structured memory/context lookup result."""

    query: str
    relevant_memories: list[str] = Field(default_factory=list)
    lesson: str


class ImplementationPlan(BaseModel):
    """Concrete build plan created by the implementation agent."""

    artifact: str
    milestones: list[str] = Field(min_length=1)
    acceptance_checks: list[str] = Field(min_length=1)


class RiskAssessment(BaseModel):
    """Quality gate produced by the critic."""

    risks: list[str] = Field(min_length=1)
    mitigations: list[str] = Field(min_length=1)
    quality_gate: str


class MemoryWrite(BaseModel):
    """Durable learning captured after collaboration."""

    memory: str
    tags: list[str] = Field(default_factory=list)
