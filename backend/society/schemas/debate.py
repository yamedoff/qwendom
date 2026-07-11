from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProposalRecord(BaseModel):
    """Proposal submitted through the native debate tool."""

    proposal_id: str = ""
    status: str = "recorded"
    agent_id: str
    proposal: str
    rationale: str
    round: int = 0
    supersedes_proposal_id: str | None = None
    target_artifact_id: str | None = None
    supports: list[str] = Field(default_factory=list)
    created_from_phase: str = "debating"


ProposalOpinionStance = Literal["support", "oppose", "uncertain", "neutral"]


class ProposalOpinionRecord(BaseModel):
    """One agent's public opinion about a proposal before or during voting."""

    agent_id: str
    proposal_id: str
    opinion: str
    stance: ProposalOpinionStance = "neutral"
    confidence: float = 0.5
    phase: str = "proposal_review"


class ChallengeRecord(BaseModel):
    """Challenge submitted against an existing proposal."""

    status: str = "challenge_recorded"
    challenger: str
    target: str
    objection: str
    suggested_revision: str
    round: int = 0


class RevisionRecord(BaseModel):
    """Revision submitted in response to a challenge."""

    status: str = "revision_recorded"
    agent_id: str
    revised_proposal: str
    changes: list[str] = Field(default_factory=list)
    round: int = 0
