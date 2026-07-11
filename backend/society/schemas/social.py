from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AgentPosition(BaseModel):
    """One agent's public stance during a society phase."""

    agent_id: str
    phase: str
    stance: Literal["support", "oppose", "uncertain", "defer", "block"]
    target: str | None = None
    reason: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    conditions: list[str] = Field(default_factory=list)


class ObjectionRecord(BaseModel):
    """A typed objection that can carry forward into execution."""

    agent_id: str
    target: str | None = None
    severity: Literal["low", "medium", "high", "critical"]
    objection: str
    resolution_condition: str
    blocks_execution: bool = False


class EndorsementRecord(BaseModel):
    """A public deferral or trust signal between agents."""

    agent_id: str
    endorsed_agent_id: str
    domain: str
    reason: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class MindChangeRecord(BaseModel):
    """Records that an agent changed position and why."""

    agent_id: str
    previous_stance: str
    new_stance: str
    trigger_agent_id: str | None = None
    reason: str


class PrivateNote(BaseModel):
    """A private note that is not public unless later published."""

    agent_id: str
    phase: str
    note: str
    may_publish: bool = False


class CollaborationAction(BaseModel):
    """A visible human-like collaboration move between agents."""

    agent_id: str
    action: Literal["help_requested", "ownership_deferred", "coalition_joined"]
    target_agent_id: str | None = None
    phase: str
    reason: str
    artifact_id: str | None = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class TrustUpdate(BaseModel):
    """One contextual trust signal emitted after collaboration."""

    evaluator_id: str
    target_agent_id: str
    domain: str
    delta: float = Field(ge=-1.0, le=1.0)
    reason: str
