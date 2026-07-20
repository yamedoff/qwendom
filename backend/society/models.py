from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


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


def derive_task_status(events: list["SocietyEvent"], fallback: str = "running") -> str:
    """Derive a replay-safe task status from the append-only event ledger.

    Terminal completion remains terminal.  A failure following an interruption
    (as emitted during graceful shutdown) is intentionally reported as failed:
    the interruption explains why, while the failure is the final outcome.
    """

    status = fallback
    completed = status in {"complete", "complete_with_warnings"}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "task_complete":
            status = "complete_with_warnings" if payload.get("acceptance_status") == "complete_with_warnings" else "complete"
            completed = True
        elif completed:
            continue
        elif event.type == "task_remediation" and status not in {"failed", "interrupted"}:
            status = "remediation"
        elif event.type == "task_interrupted" and status != "failed":
            status = "interrupted"
        elif event.type in {"task_failed", "run_failed"}:
            # Shutdown retains both events: the interruption is the terminal
            # user-facing outcome, while the paired failure records cause.
            if not (status == "interrupted" and payload.get("interrupted") is True):
                status = "failed"
        elif event.type == "user_clarification_requested" and status == "running":
            status = "waiting_for_user"
        elif event.type == "society_resumed" and status == "waiting_for_user":
            status = "running"
    return status


class Team(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    member_ids: list[str]
    voter_ids: list[str] = Field(default_factory=list)
    leader_id: str | None = None
    status: Literal["forming", "active", "dissolved"] = "forming"


class TaskRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    prompt: str
    status: Literal["queued", "running", "waiting_for_user", "complete", "complete_with_warnings", "remediation", "interrupted", "failed"] = "queued"
    team_id: str | None = None
    final_answer: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=8, max_length=4000)

    @field_validator("prompt")
    @classmethod
    def require_mission_content(cls, value: str) -> str:
        """Reject whitespace and bare field labels before a run can be created."""

        normalized = value.strip()
        labels = {"mission", "task", "prompt", "goal", "objective", "scope", "constraints", "success criteria"}
        content_lines = [line.rstrip(":").strip().casefold() for line in normalized.splitlines() if line.strip()]
        if not normalized or not content_lines or all(line in labels for line in content_lines):
            raise ValueError("Provide a mission with actionable content, not only a field label.")
        return normalized


class ClarificationRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class ToolCallPayload(BaseModel):
    """Standardized payload for tool_call events."""

    tool_name: str
    actor: str
    input_summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    mode: Literal[
        "native_agno",
        "structured_output_recovered",
        "qwen_json_retry",  # Legacy replay compatibility; new events use the generic name.
        "deterministic_no_key",
    ] = "native_agno"
    success: bool = True
