from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentProfile(BaseModel):
    """Stable work-behavior profile for a persistent society agent.

    These fields are intentionally product-facing. They define how the agent
    should act in collaboration, not a fictional human persona.
    """

    values: list[str] = Field(default_factory=list)
    communication_style: str = ""
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    decision_bias: str = ""
    default_blockers: list[str] = Field(default_factory=list)
    defers_to: dict[str, list[str]] = Field(default_factory=dict)
    failure_mode: str = ""


class SocietyAgent(BaseModel):
    """Persistent identity for an agent that can join temporary task teams."""

    id: str
    name: str
    role: str
    skills: list[str]
    profile: AgentProfile = Field(default_factory=AgentProfile)
    memory: list[str] = Field(default_factory=list)
    reputation: float = 1.0
    parent_id: str | None = None


class SocietyEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    type: str
    message: str
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


class Team(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    member_ids: list[str]
    leader_id: str | None = None
    status: Literal["forming", "active", "dissolved"] = "forming"


class TaskRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    prompt: str
    status: Literal["queued", "running", "waiting_for_user", "complete", "failed"] = "queued"
    team_id: str | None = None
    final_answer: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=8, max_length=4000)


class ClarificationRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class ToolCallPayload(BaseModel):
    """Standardized payload for tool_call events."""

    tool_name: str
    actor: str
    input_summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["native_agno", "deterministic_no_key"] = "native_agno"
    success: bool = True
