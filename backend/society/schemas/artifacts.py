from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ArtifactStatus = Literal["draft", "validated", "revised", "failed", "final"]


class ArtifactReference(BaseModel):
    """Stable pointer to an artifact stored in the session ledger."""

    id: str
    type: str
    producer: str


class ArtifactRecord(BaseModel):
    """Typed intermediate work product produced during a society run."""

    id: str
    type: str
    producer: str
    phase: str
    content: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: ArtifactStatus = "draft"
    created_at: str


class FinalDeliverable(BaseModel):
    """Traceable final answer and the artifacts used to produce it."""

    answer: str
    selected_artifact_id: str | None = None
    supporting_artifacts: list[ArtifactReference] = Field(default_factory=list)
