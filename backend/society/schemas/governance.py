from __future__ import annotations

from pydantic import BaseModel, Field


class LeaderDecision(BaseModel):
    leader_id: str = Field(description="The id of the elected leader agent")
    reason: str = Field(description="Why this agent was chosen as leader")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")


class SpawnDecision(BaseModel):
    spawn: bool = Field(description="Whether to spawn a child specialist agent")
    specialist_role: str | None = Field(default=None, description="Role description for the specialist if spawning")
    reason: str = Field(description="Why spawning or not spawning is the right decision")


class VoteDecision(BaseModel):
    choice: str = Field(description="The agent id of the best proposal")
    reason: str = Field(description="Why this proposal was chosen")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")


class CritiqueReport(BaseModel):
    critique: str = Field(description="Critical review of the winning solution")
    risks: list[str] = Field(default_factory=list, description="Identified risks and unsupported assumptions")
    improvements: list[str] = Field(default_factory=list, description="Suggested improvements")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")


class WinnerRationaleRecord(BaseModel):
    """Why the society selected the winning proposal."""

    winner_agent_id: str
    winning_proposal_id: str | None = None
    why_won: str
    supporting_votes: list[str] = Field(default_factory=list)
    critical_tradeoffs: list[str] = Field(default_factory=list)
    dissent_carried: list[str] = Field(default_factory=list)


class LeaderSynthesisRecord(BaseModel):
    """Leader's explicit synthesis of the decision, separate from ballot tallying."""

    winner_agent_id: str
    winning_proposal_id: str | None = None
    winning_proposal_summary: str
    why_won: str
    critical_tradeoffs: list[str] = Field(default_factory=list)
    carried_dissent: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    confidence: float = 0.7
    synthesis_kind: str = "leader_synthesis"


class RunFailureRecord(BaseModel):
    """Ordered explanation for why a run failed or paused."""

    phase: str
    blocking_reason: str
    missing_inputs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    system_error: str | None = None
    recoverable: bool = False
