from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


SubtaskStatus = Literal["planned", "assigned", "completed", "blocked"]
SubtaskReportStatus = Literal["completed", "blocked", "not_found"]


class SubtaskAssignment(BaseModel):
    """Result produced when a planned subtask is assigned to an agent."""

    id: str
    subtask_id: str = ""
    status: SubtaskStatus = "assigned"
    agent_id: str
    subtask: str
    deadline_step: int = 0
    assigned_by: str = ""
    why_assigned: str = ""
    done_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    blocking_if_missing: bool = False
    provenance: str = Field(default="", description="How this subtask was derived: decomposition, working_brief, coordination_brief, or leader_plan")


class SubtaskReport(BaseModel):
    """Result produced when an assigned subtask reports progress or completion."""

    id: str
    subtask_id: str = ""
    status: SubtaskReportStatus
    agent_id: str
    result: str = ""
    result_summary: str = ""
    blockers: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    outcome_status: str = ""
    provenance: str = Field(default="", description="Origin trace for the reported work")
    outcome_summary: str = Field(default="", description="One-line outcome for cockpit and recap")
