from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import SocietyAgent, SocietyEvent, derive_task_status

EvidenceStatus = Literal["complete", "partial", "missing"]


class EvidenceEnvelope(BaseModel):
    """Explains whether a read model is backed by emitted society events."""

    evidence_status: EvidenceStatus
    missing_sources: list[str] = Field(default_factory=list)


class TimelineCounts(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)


class AgentStance(BaseModel):
    agent_id: str
    stance: str
    phase: str | None = None
    reason: str | None = None
    confidence: float | None = None
    source_event_id: str


class BlockerRecord(BaseModel):
    source: str
    message: str
    agent_id: str | None = None
    required_action: str | None = None
    source_event_id: str


class ArtifactProjection(BaseModel):
    id: str | None = None
    type: str
    producer: str | None = None
    phase: str | None = None
    status: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    source_event_id: str


class TeamProjection(BaseModel):
    id: str | None = None
    member_ids: list[str] = Field(default_factory=list)
    leader_id: str | None = None
    status: str | None = None


class ParticipantProjection(BaseModel):
    agent_id: str
    role: str | None = None
    is_leader: bool = False
    current_stance: str = "unknown"
    current_action: str | None = None
    last_contribution_type: str | None = None
    last_contribution_summary: str | None = None
    status_source: str | None = None


class ToolUsageProjection(BaseModel):
    agent_id: str
    tool_name: str
    phase: str | None = None
    why_used: str | None = None
    result_summary: str | None = None
    success: bool = True
    source_event_id: str


class SpecialistSkillProjection(BaseModel):
    """Public pinned skill metadata without repository prompt content."""

    skill_id: str
    version: str
    sha256: str


class SpecialistArtifactProjection(BaseModel):
    """One artifact with additive durable-export metadata when it is a file."""

    path: str
    artifact_id: str | None = None
    filename: str | None = None
    kind: str | None = None
    media_type: str | None = None
    download_url: str | None = None
    view_url: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    producer: str | None = None
    status: str = "expected"
    validation_status: str = "pending"


class SpecialistAssignmentProjection(BaseModel):
    """Truthful UI reconstruction of one fixed specialist assignment."""

    assignment_id: str
    agent_id: str | None = None
    template_id: str
    template_version: str
    objective: str
    capabilities: list[str] = Field(default_factory=list)
    tool_ids: list[str] = Field(default_factory=list)
    skills: list[SpecialistSkillProjection] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    acceptance_requirements: list[str] = Field(default_factory=list)
    validates_assignment_ids: list[str] = Field(default_factory=list)
    status: str = "selected"
    sandbox_status: str = "not_started"
    artifacts: list[SpecialistArtifactProjection] = Field(default_factory=list)
    blocker: str | None = None


class SpecialistExecutionProjection(BaseModel):
    """Fixed-specialist selection, graph, execution, and readiness evidence."""

    strategy: str = "fixed_specialists"
    selection_rationale: str | None = None
    correction_count: int = 0
    assignments: list[SpecialistAssignmentProjection] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    development_readiness: str = "development_only"


class DelegationProjection(BaseModel):
    subtask_id: str
    assigned_by: str | None = None
    agent_id: str
    objective: str
    why_assigned: str | None = None
    done_criteria: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    status: str = "planned"
    result_summary: str | None = None
    outcome_status: str | None = None
    outcome_summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    provenance: str | None = None
    blocking_if_missing: bool = False


class WinnerRationaleProjection(BaseModel):
    winner_agent_id: str
    winning_proposal_id: str | None = None
    why_won: str
    supporting_votes: list[str] = Field(default_factory=list)
    critical_tradeoffs: list[str] = Field(default_factory=list)
    dissent_carried: list[str] = Field(default_factory=list)


class LeaderSynthesisProjection(BaseModel):
    winner_agent_id: str
    winning_proposal_id: str | None = None
    winning_proposal_summary: str
    why_won: str
    critical_tradeoffs: list[str] = Field(default_factory=list)
    carried_dissent: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    confidence: float | None = None
    synthesis_kind: str = "leader_synthesis"
    source_event_id: str


class FailureProjection(BaseModel):
    phase: str
    blocking_reason: str
    missing_inputs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    system_error: str | None = None
    recoverable: bool = False


class DemoProofProjection(BaseModel):
    """Observed proof points for a judge-facing Agent Society run."""

    verified: bool = False
    markers: dict[str, bool] = Field(default_factory=dict)
    competency_roles: list[str] = Field(default_factory=list)
    evidence_subtask_ids: list[str] = Field(default_factory=list)
    leader_id: str | None = None
    carried_dissent_count: int = 0
    final_artifact_id: str | None = None
    missing_markers: list[str] = Field(default_factory=list)
    source_event_id: str


class RunCockpit(EvidenceEnvelope):
    task_id: str
    prompt: str | None = None
    status: str
    current_phase: str
    team: TeamProjection | None = None
    participants: list[ParticipantProjection] = Field(default_factory=list)
    leader_rationale: str | None = None
    latest_agent_stances: list[AgentStance] = Field(default_factory=list)
    gates: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[BlockerRecord] = Field(default_factory=list)
    artifacts: list[ArtifactProjection] = Field(default_factory=list)
    delegation_summary: list[DelegationProjection] = Field(default_factory=list)
    tool_usage_summary: list[ToolUsageProjection] = Field(default_factory=list)
    specialist_execution: SpecialistExecutionProjection | None = None
    trace_status: str = "live"
    generated_at: str | None = None
    stale_reason: str | None = None
    failure: FailureProjection | None = None
    demo_proof: DemoProofProjection | None = None
    final_answer: str | None = None
    timeline: TimelineCounts = Field(default_factory=TimelineCounts)


class ProposalProjection(BaseModel):
    proposal_id: str | None = None
    agent_id: str
    proposal: str
    rationale: str | None = None
    supersedes_proposal_id: str | None = None
    created_from_phase: str | None = None
    source_event_id: str


class ProposalOpinionProjection(BaseModel):
    proposal_id: str
    agent_id: str
    stance: str
    opinion: str
    confidence: float | None = None
    phase: str | None = None
    source_event_id: str


class CritiqueProjection(BaseModel):
    critic_id: str | None = None
    target: str | None = None
    critique: str
    risks: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    source_event_id: str


class RevisionProjection(BaseModel):
    agent_id: str
    revised_proposal: str
    changes: list[str] = Field(default_factory=list)
    source_event_id: str


class BallotProjection(BaseModel):
    voter_id: str
    choice: str
    reason: str | None = None
    confidence: float | None = None
    source_event_id: str


class DecisionReview(EvidenceEnvelope):
    task_id: str
    current_phase: str
    proposals: list[ProposalProjection] = Field(default_factory=list)
    proposal_opinions: list[ProposalOpinionProjection] = Field(default_factory=list)
    critiques: list[CritiqueProjection] = Field(default_factory=list)
    revisions: list[RevisionProjection] = Field(default_factory=list)
    ballots: list[BallotProjection] = Field(default_factory=list)
    selected_winner: str | None = None
    selected_proposal_id: str | None = None
    winner_rationale: WinnerRationaleProjection | None = None
    leader_synthesis: LeaderSynthesisProjection | None = None
    blocking_objections: list[str] = Field(default_factory=list)
    non_blocking_dissent: list[str] = Field(default_factory=list)
    unresolved_dissent: list[str] = Field(default_factory=list)
    supporting_artifacts: list[ArtifactProjection] = Field(default_factory=list)


class RunRecap(EvidenceEnvelope):
    task_id: str
    status: str
    duration_seconds: float | None = None
    turns: int = 0
    events: int = 0
    plan_changes: list[str] = Field(default_factory=list)
    mind_changes: list[dict[str, Any]] = Field(default_factory=list)
    dissents: list[str] = Field(default_factory=list)
    saved_lessons: list[str] = Field(default_factory=list)
    trust_reputation_changes: list[dict[str, Any]] = Field(default_factory=list)
    social_deltas: list[dict[str, Any]] = Field(default_factory=list)
    delegation_outcomes: list[DelegationProjection] = Field(default_factory=list)
    completion_outcome: str = "partial"
    failure: FailureProjection | None = None
    metrics: dict[str, Any] | None = None
    final_answer: str | None = None


class AgentDossier(EvidenceEnvelope):
    agent: SocietyAgent
    task_id: str | None = None
    this_run_summary: dict[str, Any] = Field(default_factory=dict)
    this_run_timeline: list[dict[str, Any]] = Field(default_factory=list)
    run_lessons: list[str] = Field(default_factory=list)
    published_notes: list[str] = Field(default_factory=list)
    run_tool_usage: list[ToolUsageProjection] = Field(default_factory=list)
    run_delegation: list[DelegationProjection] = Field(default_factory=list)
    reputation: float
    trust: list[dict[str, Any]] = Field(default_factory=list)
    behavioral_tendencies: list[str] = Field(default_factory=list)
    recent_stances: list[AgentStance] = Field(default_factory=list)


def _payload(event: SocietyEvent) -> dict[str, Any]:
    return event.payload if isinstance(event.payload, dict) else {}


def _first_prompt(events: list[SocietyEvent]) -> str | None:
    for event in events:
        prompt = _payload(event).get("prompt")
        if isinstance(prompt, str):
            return prompt
    return None


def _task_status(events: list[SocietyEvent], fallback: str = "running") -> str:
    return derive_task_status(events, fallback)


def _phase(events: list[SocietyEvent]) -> str:
    phase = "setup"
    for event in events:
        payload = _payload(event)
        if event.type == "task_complete":
            phase = "complete"
        elif event.type == "task_remediation":
            phase = "remediation"
        elif event.type in {"task_failed", "run_failed"}:
            phase = str(payload.get("phase") or "failed")
        elif event.type == "acceptance_evidence_evaluated":
            phase = "acceptance_evaluation"
        elif event.type == "validation_gate_completed":
            phase = "validation"
        elif event.type in {"peer_monitor_report", "artifact_section_critiqued", "shared_artifact_revised"}:
            phase = "review"
        elif event.type in {"vote_cast", "ballots_tallied", "solution_selected", "winner_selected", "leader_synthesis"}:
            phase = "decision"
        elif event.type in {"proposal_challenged", "proposal_revised", "debate_round_completed", "agent_position_stated", "agent_proposal_submitted"}:
            phase = "work_review"
        elif event.type in {"subtasks_assigned_from_brief", "delegation_assigned"}:
            phase = "subtask_assignment"
        elif event.type.startswith("specialist_selection_"):
            phase = "team_composition"
        elif event.type in {"specialist_invocation_approved", "composition_assignment_materialized"}:
            phase = "specialist_execution"
        elif event.type.startswith("work_node_") or event.type.startswith("agentbay_") or event.type.startswith("composition_assignment_cleanup_"):
            phase = "specialist_execution"
        elif event.type == "leader_elected":
            phase = "leader_election"
        elif event.type == "working_brief_finalized":
            phase = "working_brief"
        elif event.type in {"readiness_vote_cast", "readiness_vote_tallied"}:
            phase = "readiness_vote"
        elif event.type in {"goal_discussion_started", "conversation_turn", "agent_goal_opinion"}:
            phase = "goal_discussion"
    return phase


def _team(events: list[SocietyEvent]) -> TeamProjection | None:
    for event in reversed(events):
        raw_team = _payload(event).get("team")
        if isinstance(raw_team, dict):
            return TeamProjection(
                id=_str_or_none(raw_team.get("id")),
                member_ids=[str(item) for item in raw_team.get("member_ids", []) if item is not None],
                leader_id=_str_or_none(raw_team.get("leader_id")),
                status=_str_or_none(raw_team.get("status")),
            )
    return None


def _latest_stances(events: list[SocietyEvent]) -> list[AgentStance]:
    by_agent: dict[str, AgentStance] = {}
    for event in events:
        payload = _payload(event)
        agent_id = _str_or_none(payload.get("agent_id")) or event.actor
        if not agent_id:
            continue
        stance = _str_or_none(payload.get("stance")) or _stance_from_event(event)
        if not stance:
            continue
        by_agent[agent_id] = AgentStance(
            agent_id=agent_id,
            stance=stance,
            phase=_str_or_none(payload.get("phase")),
            reason=_str_or_none(payload.get("reason") or payload.get("interpretation") or payload.get("says")),
            confidence=_float_or_none(payload.get("confidence")),
            source_event_id=event.id,
        )
    return list(by_agent.values())


def _stance_from_event(event: SocietyEvent) -> str | None:
    if event.type == "vote_cast":
        return "support"
    if event.type == "agent_changed_mind":
        return _str_or_none(_payload(event).get("new_stance"))
    return None


def _blockers(events: list[SocietyEvent]) -> list[BlockerRecord]:
    blockers: list[BlockerRecord] = []
    for event in events:
        payload = _payload(event)
        if event.type == "user_clarification_requested":
            blockers.append(
                BlockerRecord(
                    source=event.type,
                    message=event.message,
                    agent_id=event.actor,
                    required_action=_str_or_none(payload.get("question") or payload.get("clarification") or payload.get("required_clarification")),
                    source_event_id=event.id,
                )
            )
        if event.type == "readiness_vote_cast" and payload.get("critical_blocker") is True:
            blockers.append(
                BlockerRecord(
                    source=event.type,
                    message=_str_or_none(payload.get("reason")) or event.message,
                    agent_id=_str_or_none(payload.get("agent_id")) or event.actor,
                    required_action=_str_or_none(payload.get("required_clarification")),
                    source_event_id=event.id,
                )
            )
        if event.type == "readiness_vote_tallied":
            for item in _list_of_strings(payload.get("blockers")):
                blockers.append(BlockerRecord(source=event.type, message=item, source_event_id=event.id))
        if event.type == "working_brief_finalized":
            for item in _list_of_strings(payload.get("blocked_items")):
                blockers.append(BlockerRecord(source=event.type, message=item, source_event_id=event.id))
    # A blocker event is an audit record, not proof that the condition is still
    # active.  Keep the ledger intact while omitting claims later superseded by
    # explicit execution/validation evidence from the active read model.
    return [item for item in blockers if not _claim_resolved_by_validation(item.message, events)]


def _validation_proof_text(events: list[SocietyEvent]) -> str:
    """Return text from passed validator events only.

    Projection code must not treat a shortened preview as a failed validation,
    nor let an earlier failure outrank a later validator result.  The emitted
    validator payload is the narrowest existing source for these facts.
    """

    parts: list[str] = []
    for event in events:
        if event.type not in {"artifact_validated", "local_independent_validation_reported"}:
            continue
        payload = _payload(event)
        if payload.get("passed") is not True:
            continue
        parts.append(event.message)
        for key in ("summary", "result", "reason", "checks", "validation", "evidence"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(item) for item in value)
            elif isinstance(value, dict):
                parts.extend(str(item) for item in value.values())
    return " ".join(parts).lower()


def _has_verified_artifact_evidence(events: list[SocietyEvent]) -> bool:
    """Whether the ledger contains both an export and a passed validation."""

    return (
        any(event.type == "agentbay_artifact_exported" for event in events)
        and bool(_validation_proof_text(events))
    )


def _claim_resolved_by_validation(claim: str, events: list[SocietyEvent]) -> bool:
    """Suppress only stale, named proof-gap claims after authoritative proof.

    This deliberately does not clear ordinary product, scope, or dependency
    blockers.  It is limited to the proof gaps that an emitted artifact
    validation can actually supersede.
    """

    normalized = claim.lower()
    proof = _validation_proof_text(events)
    if not proof:
        return False
    if any(token in normalized for token in ("missing artifact", "artifact is missing", "artifact evidence is missing")):
        return _has_verified_artifact_evidence(events)
    if "truncat" in normalized and "validation" in normalized:
        return _has_verified_artifact_evidence(events)
    if "viewport" in normalized and any(token in normalized for token in ("dual", "mobile", "desktop")):
        return "viewport" in proof and ("mobile" in proof or "desktop" in proof or "dual" in proof)
    if "accessibility" in normalized:
        return "accessibility" in proof
    return False


def _gates(events: list[SocietyEvent]) -> list[dict[str, Any]]:
    return [
        {"type": event.type, "message": event.message, "payload": _payload(event), "created_at": event.created_at, "source_event_id": event.id}
        for event in events
        if event.type in {"readiness_vote_tallied", "validation_gate_completed", "workflow_checkpoint", "workflow_completed"}
    ]


def _artifacts(events: list[SocietyEvent]) -> list[ArtifactProjection]:
    artifacts: list[ArtifactProjection] = []
    for event in events:
        payload = _payload(event)
        if event.type == "working_brief_finalized":
            artifacts.append(ArtifactProjection(type="working_brief", phase="working_brief", content=payload, source_event_id=event.id))
        elif event.type == "meeting_recap":
            artifacts.append(ArtifactProjection(type="meeting_recap", phase="recap", content=payload, source_event_id=event.id))
        elif event.type == "agent_proposal_submitted":
            artifacts.append(
                ArtifactProjection(
                    id=_str_or_none(payload.get("proposal_id")),
                    type="proposal",
                    producer=_str_or_none(payload.get("agent_id")) or event.actor,
                    phase=_str_or_none(payload.get("created_from_phase")) or "debating",
                    status=_str_or_none(payload.get("status")) or "recorded",
                    content=payload,
                    source_event_id=event.id,
                )
            )
        elif event.type == "winner_selected":
            artifacts.append(
                ArtifactProjection(
                    id=_str_or_none(payload.get("winning_proposal_id")),
                    type="winner_rationale",
                    producer=_str_or_none(payload.get("winner_agent_id")) or event.actor,
                    phase="decision",
                    status="final",
                    content=payload,
                    source_event_id=event.id,
                )
            )
        elif event.type == "leader_synthesis":
            artifacts.append(
                ArtifactProjection(
                    id=_str_or_none(payload.get("winning_proposal_id")),
                    type="leader_synthesis",
                    producer=_str_or_none(payload.get("winner_agent_id")) or event.actor,
                    phase="decision",
                    status="final",
                    content=payload,
                    source_event_id=event.id,
                )
            )
        elif event.type == "tool_call":
            result = payload.get("result")
            tool_name = _str_or_none(payload.get("tool_name")) or "tool_call"
            if isinstance(result, dict) and result:
                artifacts.append(
                    ArtifactProjection(
                        type=tool_name,
                        producer=_str_or_none(payload.get("actor")) or event.actor,
                        phase="tool_call",
                        status="validated" if payload.get("success") else "failed",
                        content=result,
                        source_event_id=event.id,
                    )
                )
        elif event.type == "agentbay_artifact_exported":
            ref = payload.get("artifact_ref")
            ref_data = ref if isinstance(ref, dict) else {}
            artifact_id = _str_or_none(ref_data.get("id") or payload.get("artifact_id"))
            artifact_path = _str_or_none(
                payload.get("workspace_relative_path") or ref_data.get("workspace_relative_path") or payload.get("path")
            )
            if artifact_id or artifact_path:
                artifacts.append(
                    ArtifactProjection(
                        id=artifact_id,
                        type="durable_artifact",
                        producer=_str_or_none(ref_data.get("producer") or payload.get("producer") or payload.get("role_key")),
                        phase="specialist_execution",
                        # Export is a fact; per-artifact validation remains on
                        # the Artifact page unless a validator names this file.
                        status="exported",
                        content={"path": artifact_path, "sha256": _str_or_none(ref_data.get("sha256") or payload.get("sha256"))},
                        source_event_id=event.id,
                    )
                )
    return artifacts


def _tool_usage_summary(events: list[SocietyEvent]) -> list[ToolUsageProjection]:
    usage: list[ToolUsageProjection] = []
    for event in events:
        if event.type != "tool_call":
            continue
        payload = _payload(event)
        tool_name = _str_or_none(payload.get("tool_name"))
        actor = _str_or_none(payload.get("actor")) or event.actor
        if not tool_name or not actor:
            continue
        input_summary = _str_or_none(payload.get("input_summary"))
        result = payload.get("result")
        result_summary = None
        if isinstance(result, dict) and result:
            result_summary = _summarize_result(result)
        usage.append(
            ToolUsageProjection(
                agent_id=actor,
                tool_name=tool_name,
                phase=_phase(events[: events.index(event) + 1]),
                why_used=input_summary,
                result_summary=result_summary,
                success=bool(payload.get("success", True)),
                source_event_id=event.id,
            )
        )
    return usage


def _summarize_result(result: dict[str, Any]) -> str | None:
    for key in ("reason", "critique", "proposal", "revised_proposal", "answer", "summary", "result", "memory"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value[:240]
    if result:
        return ", ".join(str(key) for key in list(result.keys())[:4])
    return None


def _leader_rationale(events: list[SocietyEvent]) -> str | None:
    for event in reversed(events):
        if event.type == "leader_elected":
            return _str_or_none(_payload(event).get("reason")) or event.message
    return None


def _participants(events: list[SocietyEvent]) -> list[ParticipantProjection]:
    team = _team(events)
    if team is None:
        return []
    stance_by_agent = {stance.agent_id: stance for stance in _latest_stances(events)}
    last_event_by_agent: dict[str, SocietyEvent] = {}
    for event in events:
        payload = _payload(event)
        agent_id = _str_or_none(payload.get("agent_id")) or event.actor
        if agent_id:
            last_event_by_agent[agent_id] = event
    participants: list[ParticipantProjection] = []
    for agent_id in team.member_ids:
        stance = stance_by_agent.get(agent_id)
        last_event = last_event_by_agent.get(agent_id)
        role = None
        joined = next((event for event in events if event.type == "team_formed"), None)
        if joined:
            members = _payload(joined).get("team")
            if isinstance(members, dict):
                role = _role_for_agent(members, agent_id)
        participants.append(
            ParticipantProjection(
                agent_id=agent_id,
                role=role,
                is_leader=agent_id == team.leader_id,
                current_stance=stance.stance if stance else "unknown",
                current_action=last_event.type if last_event else None,
                last_contribution_type=last_event.type if last_event else None,
                last_contribution_summary=last_event.message if last_event else None,
                status_source=last_event.id if last_event else None,
            )
        )
    return participants


def _role_for_agent(team_payload: dict[str, Any], agent_id: str) -> str | None:
    raw_members = team_payload.get("member_ids")
    if isinstance(raw_members, list) and agent_id in raw_members:
        return agent_id
    return None


def _delegation_summary(events: list[SocietyEvent]) -> list[DelegationProjection]:
    records: dict[str, DelegationProjection] = {}
    for event in events:
        payload = _payload(event)
        if event.type in {"subtasks_assigned_from_brief", "delegation_assigned"}:
            assignments = payload.get("assignments")
            if isinstance(assignments, list):
                for item in assignments:
                    if not isinstance(item, dict):
                        continue
                    subtask_id = _str_or_none(item.get("id")) or _str_or_none(item.get("subtask_id"))
                    agent_id = _str_or_none(item.get("agent_id"))
                    objective = _str_or_none(item.get("objective")) or _str_or_none(item.get("subtask"))
                    if not subtask_id or not agent_id or not objective:
                        continue
                    records[subtask_id] = DelegationProjection(
                        subtask_id=subtask_id,
                        assigned_by=_str_or_none(payload.get("leader_id")) or _str_or_none(item.get("assigned_by")) or event.actor,
                        agent_id=agent_id,
                        objective=objective,
                        why_assigned=_str_or_none(item.get("why_assigned")),
                        done_criteria=_list_of_strings(item.get("done_criteria")),
                        blockers=_list_of_strings(item.get("blockers")),
                        status=_str_or_none(item.get("status")) or "assigned",
                        result_summary=_str_or_none(item.get("result_summary")),
                        outcome_status=_str_or_none(item.get("outcome_status")),
                        outcome_summary=_str_or_none(item.get("outcome_summary")),
                        evidence_refs=_list_of_strings(item.get("evidence_refs")),
                        provenance=_str_or_none(item.get("provenance")),
                        blocking_if_missing=bool(item.get("blocking_if_missing", False)),
                    )
            else:
                subtask_id = _str_or_none(payload.get("subtask_id")) or _str_or_none(payload.get("id"))
                agent_id = _str_or_none(payload.get("agent_id"))
                objective = _str_or_none(payload.get("objective")) or _str_or_none(payload.get("subtask"))
                if subtask_id and agent_id and objective:
                    records[subtask_id] = DelegationProjection(
                        subtask_id=subtask_id,
                        assigned_by=_str_or_none(payload.get("assigned_by")) or event.actor,
                        agent_id=agent_id,
                        objective=objective,
                        why_assigned=_str_or_none(payload.get("why_assigned")),
                        done_criteria=_list_of_strings(payload.get("done_criteria")),
                        blockers=_list_of_strings(payload.get("blockers")),
                        status=_str_or_none(payload.get("status")) or "assigned",
                        result_summary=_str_or_none(payload.get("result_summary")),
                        outcome_status=_str_or_none(payload.get("outcome_status")),
                        outcome_summary=_str_or_none(payload.get("outcome_summary")),
                        evidence_refs=_list_of_strings(payload.get("evidence_refs")),
                        provenance=_str_or_none(payload.get("provenance")),
                        blocking_if_missing=bool(payload.get("blocking_if_missing", False)),
                    )
        elif event.type in {"delegation_reported"}:
            subtask_id = _str_or_none(payload.get("subtask_id")) or _str_or_none(payload.get("id"))
            if subtask_id and subtask_id in records:
                existing = records[subtask_id]
                existing.status = _str_or_none(payload.get("status")) or existing.status
                existing.blockers = _list_of_strings(payload.get("blockers")) or existing.blockers
                existing.result_summary = _str_or_none(payload.get("result_summary") or payload.get("result")) or existing.result_summary
                existing.outcome_status = _str_or_none(payload.get("outcome_status")) or existing.outcome_status
                existing.outcome_summary = _str_or_none(payload.get("outcome_summary")) or existing.outcome_summary
                existing.evidence_refs = _list_of_strings(payload.get("evidence_refs")) or existing.evidence_refs
            elif subtask_id:
                records[subtask_id] = DelegationProjection(
                    subtask_id=subtask_id,
                    agent_id=_str_or_none(payload.get("agent_id")) or event.actor or "",
                    objective="",
                    status=_str_or_none(payload.get("status")) or "reported",
                    result_summary=_str_or_none(payload.get("result_summary") or payload.get("result")),
                    outcome_status=_str_or_none(payload.get("outcome_status")),
                    outcome_summary=_str_or_none(payload.get("outcome_summary")),
                    evidence_refs=_list_of_strings(payload.get("evidence_refs")),
                    provenance=_str_or_none(payload.get("provenance")),
                )
    return list(records.values())


def _failure(events: list[SocietyEvent]) -> FailureProjection | None:
    for event in reversed(events):
        if event.type in {"run_failed", "task_failed"}:
            payload = _payload(event)
            return FailureProjection(
                phase=_str_or_none(payload.get("phase")) or "unknown",
                blocking_reason=_str_or_none(payload.get("blocking_reason")) or event.message,
                missing_inputs=_list_of_strings(payload.get("missing_inputs")),
                missing_evidence=_list_of_strings(payload.get("missing_evidence")),
                system_error=_str_or_none(payload.get("system_error") or payload.get("error")),
                recoverable=bool(payload.get("recoverable", False)),
            )
    return None


def _demo_proof(events: list[SocietyEvent]) -> DemoProofProjection | None:
    """Project the latest emitted proof checklist without inferring missing facts."""

    for event in reversed(events):
        if event.type != "demo_proof_verified":
            continue
        payload = _payload(event)
        marker_payload = payload.get("markers")
        markers = {
            str(name): bool(present)
            for name, present in marker_payload.items()
        } if isinstance(marker_payload, dict) else {}
        return DemoProofProjection(
            verified=bool(payload.get("verified", False)),
            markers=markers,
            competency_roles=_list_of_strings(payload.get("competency_roles")),
            evidence_subtask_ids=_list_of_strings(payload.get("evidence_subtask_ids")),
            leader_id=_str_or_none(payload.get("leader_id")),
            carried_dissent_count=int(payload.get("carried_dissent_count", 0) or 0),
            final_artifact_id=_str_or_none(payload.get("final_artifact_id")),
            missing_markers=_list_of_strings(payload.get("missing_markers")),
            source_event_id=event.id,
        )
    return None


def _specialist_execution(task_id: str, events: list[SocietyEvent]) -> SpecialistExecutionProjection | None:
    """Reconstruct fixed specialist execution from persisted public events."""

    accepted_event: SocietyEvent | None = None
    correction_count = 0
    blockers: list[str] = []
    for event in events:
        payload = _payload(event)
        if event.type == "specialist_selection_rejected":
            correction_count += 1
            for blocker in payload.get("blockers", []):
                if isinstance(blocker, dict):
                    message = _str_or_none(blocker.get("message"))
                    if message:
                        blockers.append(message)
        elif event.type == "specialist_selection_accepted":
            accepted_event = event

    if accepted_event is None:
        if correction_count == 0:
            return None
        return SpecialistExecutionProjection(
            correction_count=correction_count,
            blockers=blockers,
            development_readiness="blocked",
        )

    accepted_payload = _payload(accepted_event)
    assignment_by_id: dict[str, SpecialistAssignmentProjection] = {}
    for raw in accepted_payload.get("assignments", []):
        if not isinstance(raw, dict):
            continue
        assignment_id = _str_or_none(raw.get("assignment_id"))
        template_id = _str_or_none(raw.get("template_id"))
        template_version = _str_or_none(raw.get("template_version"))
        objective = _str_or_none(raw.get("objective"))
        if not all((assignment_id, template_id, template_version, objective)):
            continue
        skill_ids = _list_of_strings(raw.get("skill_ids"))
        skill_versions = _list_of_strings(raw.get("skill_versions"))
        skill_hashes = _list_of_strings(raw.get("skill_hashes"))
        skills = [
            SpecialistSkillProjection(skill_id=skill_id, version=version, sha256=sha256)
            for skill_id, version, sha256 in zip(skill_ids, skill_versions, skill_hashes)
        ]
        assignment_by_id[assignment_id] = SpecialistAssignmentProjection(
            assignment_id=assignment_id,
            template_id=template_id,
            template_version=template_version,
            objective=objective,
            capabilities=_list_of_strings(raw.get("capabilities")),
            tool_ids=_list_of_strings(raw.get("tool_ids")),
            skills=skills,
            depends_on=_list_of_strings(raw.get("depends_on")),
            acceptance_requirements=_list_of_strings(raw.get("acceptance_requirements")),
            validates_assignment_ids=_list_of_strings(raw.get("validates_assignment_ids")),
            artifacts=[
                SpecialistArtifactProjection(path=path)
                for path in _list_of_strings(raw.get("owned_artifacts"))
            ],
        )

    for event in events[events.index(accepted_event) + 1:]:
        payload = _payload(event)
        assignment_id = _str_or_none(payload.get("assignment_id")) or _str_or_none(payload.get("node_id"))
        assignment = assignment_by_id.get(assignment_id or "")
        if assignment is None:
            continue
        if event.type == "specialist_invocation_approved":
            assignment.status = "approved"
        elif event.type == "specialist_invocation_started":
            assignment.status = "running"
        elif event.type == "composition_assignment_materialized":
            assignment.agent_id = _str_or_none(payload.get("id") or payload.get("agent_id"))
            assignment.status = "materialized"
        elif event.type == "work_node_started":
            assignment.status = "running"
        elif event.type == "work_node_completed":
            assignment.status = "completed"
        elif event.type in {"work_node_blocked", "work_node_failed", "work_node_canceled"}:
            assignment.status = event.type.removeprefix("work_node_")
            assignment.blocker = _str_or_none(payload.get("message") or payload.get("error") or payload.get("reason"))
        elif event.type == "agentbay_start_succeeded":
            assignment.sandbox_status = "active"
        elif event.type == "composition_assignment_cleanup_completed":
            assignment.sandbox_status = "closed"
        elif event.type == "composition_assignment_cleanup_warning":
            assignment.sandbox_status = "cleanup_warning"
            assignment.blocker = "Sandbox cleanup reported a warning."
        elif event.type == "agentbay_artifact_exported":
            artifact_ref = payload.get("artifact_ref")
            ref = artifact_ref if isinstance(artifact_ref, dict) else {}
            path = _str_or_none(
                payload.get("workspace_relative_path")
                or payload.get("path")
                or ref.get("workspace_relative_path")
                or payload.get("local_path")
                or artifact_ref
            )
            if path:
                normalized_path = str(path).replace("\\", "/")
                candidate_path = PurePosixPath(normalized_path)
                windows_path = PureWindowsPath(normalized_path)
                if candidate_path.is_absolute() or bool(windows_path.drive) or ".." in candidate_path.parts:
                    # The durable API, rather than event internals, owns storage
                    # paths. Never project an absolute local path into Cockpit.
                    normalized_path = _str_or_none(ref.get("id")) or "exported-artifact"
                path = normalized_path
                artifact = next((item for item in assignment.artifacts if item.path == path), None)
                if artifact is None:
                    artifact = SpecialistArtifactProjection(path=path)
                    assignment.artifacts.append(artifact)
                artifact_id = _str_or_none(ref.get("id") or payload.get("artifact_id"))
                artifact.artifact_id = artifact_id
                artifact.filename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                artifact.kind = _str_or_none(ref.get("type") or payload.get("artifact_kind"))
                artifact.producer = _str_or_none(ref.get("producer") or payload.get("producer") or payload.get("role_key"))
                artifact.download_url = f"/tasks/{task_id}/artifacts/{artifact_id}" if artifact_id else None
                # The API determines the final safe media type from the durable
                # file, so projections advertise only the stable view route.
                artifact.view_url = f"/tasks/{task_id}/artifacts/{artifact_id}/view" if artifact_id else None
                artifact.sha256 = _str_or_none(ref.get("sha256") or payload.get("sha256") or payload.get("content_hash"))
                size_bytes = ref.get("size_bytes") or payload.get("size_bytes")
                artifact.size_bytes = size_bytes if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) else None
                artifact.status = "exported"
        elif event.type in {"artifact_validated", "local_independent_validation_reported"}:
            for target_id in assignment.validates_assignment_ids:
                target = assignment_by_id.get(target_id)
                if target is not None:
                    for artifact in target.artifacts:
                        artifact.validation_status = "passed" if payload.get("passed", True) else "failed"

    assignment_values = list(assignment_by_id.values())
    all_complete = bool(assignment_values) and all(item.status == "completed" for item in assignment_values)
    all_closed = all(item.sandbox_status in {"closed", "not_started"} for item in assignment_values)
    # Rejected selections remain visible as history, but a later accepted and
    # fully completed run is not kept blocked by a corrected proposal.
    readiness = "development_gate_passed" if all_complete and all_closed else "development_only"
    if any(item.status in {"blocked", "failed", "canceled"} for item in assignment_values):
        readiness = "blocked"
    return SpecialistExecutionProjection(
        selection_rationale=_str_or_none(accepted_payload.get("selection_rationale")),
        correction_count=correction_count,
        assignments=assignment_values,
        blockers=blockers,
        development_readiness=readiness,
    )


def _trace_status(events: list[SocietyEvent], status: str) -> str:
    if status == "interrupted":
        return "interrupted"
    if status == "failed" or any(event.type in {"task_failed", "run_failed"} for event in events):
        return "failed"
    if status in {"complete", "complete_with_warnings"}:
        return "complete"
    if status == "remediation":
        return "remediation"
    if any(event.type == "user_clarification_requested" for event in events):
        return "blocked"
    return "live"


def _winner_rationale(events: list[SocietyEvent]) -> WinnerRationaleProjection | None:
    for event in reversed(events):
        if event.type == "winner_selected":
            payload = _payload(event)
            return WinnerRationaleProjection(
                winner_agent_id=_str_or_none(payload.get("winner_agent_id")) or event.actor or "",
                winning_proposal_id=_str_or_none(payload.get("winning_proposal_id")),
                why_won=_str_or_none(payload.get("why_won")) or event.message,
                supporting_votes=_list_of_strings(payload.get("supporting_votes")),
                critical_tradeoffs=_list_of_strings(payload.get("critical_tradeoffs")),
                dissent_carried=_list_of_strings(payload.get("dissent_carried")),
            )
    return None


def _final_answer(events: list[SocietyEvent]) -> str | None:
    for event in reversed(events):
        answer = _payload(event).get("answer")
        if event.type == "task_complete" and isinstance(answer, str):
            return answer
    return None


def _timeline(events: list[SocietyEvent]) -> TimelineCounts:
    return TimelineCounts(total=len(events), by_type=dict(Counter(event.type for event in events)))


def _evidence(required: dict[str, bool]) -> tuple[EvidenceStatus, list[str]]:
    missing = [name for name, present in required.items() if not present]
    if not missing:
        return "complete", []
    if any(required.values()):
        return "partial", missing
    return "missing", missing


def project_cockpit(task_id: str, events: list[SocietyEvent], task_summary: dict[str, Any] | None = None) -> RunCockpit:
    # The immutable ledger wins whenever it is available; an in-memory task
    # can lag a terminal shutdown event during process teardown.
    status = _task_status(events) if events else str((task_summary or {}).get("status") or "running")
    required = {
        "task_received": any(event.type == "task_received" for event in events),
        "team_formed": any(event.type == "team_formed" for event in events),
        "latest_agent_stances": bool(_latest_stances(events)),
        "gates": bool(_gates(events)),
    }
    evidence_status, missing_sources = _evidence(required)
    return RunCockpit(
        task_id=task_id,
        prompt=_str_or_none((task_summary or {}).get("prompt")) or _first_prompt(events),
        status=status,
        current_phase=_phase(events),
        team=_team(events),
        participants=_participants(events),
        leader_rationale=_leader_rationale(events),
        latest_agent_stances=_latest_stances(events),
        gates=_gates(events),
        blockers=_blockers(events),
        artifacts=_artifacts(events),
        delegation_summary=_delegation_summary(events),
        tool_usage_summary=_tool_usage_summary(events),
        specialist_execution=_specialist_execution(task_id, events),
        trace_status=_trace_status(events, status),
        generated_at=datetime.utcnow().isoformat(),
        stale_reason=None,
        failure=_failure(events),
        demo_proof=_demo_proof(events),
        final_answer=_str_or_none((task_summary or {}).get("final_answer")) or _final_answer(events),
        timeline=_timeline(events),
        evidence_status=evidence_status,
        missing_sources=missing_sources,
    )


def project_review(task_id: str, events: list[SocietyEvent]) -> DecisionReview:
    proposals: list[ProposalProjection] = []
    proposal_opinions: list[ProposalOpinionProjection] = []
    critiques: list[CritiqueProjection] = []
    revisions: list[RevisionProjection] = []
    ballots: list[BallotProjection] = []
    selected_winner: str | None = None
    selected_proposal_id: str | None = None
    winner_rationale: WinnerRationaleProjection | None = None
    leader_synthesis: LeaderSynthesisProjection | None = None
    blocking_objections: list[str] = []
    non_blocking_dissent: list[str] = []
    unresolved_dissent: list[str] = []
    proposal_ids_by_agent: dict[str, str] = {}

    for event in events:
        payload = _payload(event)
        result = payload.get("result") if event.type == "tool_call" else None
        result_payload = result if isinstance(result, dict) else payload
        tool_name = _str_or_none(payload.get("tool_name"))

        if event.type == "agent_proposal_submitted":
            agent_id = _str_or_none(payload.get("agent_id")) or event.actor
            proposal = _str_or_none(payload.get("proposal"))
            pid = _str_or_none(payload.get("proposal_id"))
            if agent_id and proposal:
                proposals.append(
                    ProposalProjection(
                        proposal_id=pid,
                        agent_id=agent_id,
                        proposal=proposal,
                        rationale=_str_or_none(payload.get("rationale")),
                        supersedes_proposal_id=_str_or_none(payload.get("supersedes_proposal_id")),
                        created_from_phase=_str_or_none(payload.get("created_from_phase")),
                        source_event_id=event.id,
                    )
                )
                if pid and agent_id:
                    proposal_ids_by_agent[agent_id] = pid
        elif event.type == "tool_call" and tool_name in {"propose", "submit_proposal"}:
            agent_id = _str_or_none(result_payload.get("agent_id")) or event.actor
            proposal = _str_or_none(result_payload.get("proposal"))
            if agent_id and proposal and agent_id not in proposal_ids_by_agent:
                proposals.append(ProposalProjection(agent_id=agent_id, proposal=proposal, rationale=_str_or_none(result_payload.get("rationale")), source_event_id=event.id))
        elif event.type == "proposal_opinion_recorded":
            pid = _str_or_none(payload.get("proposal_id"))
            aid = _str_or_none(payload.get("agent_id")) or event.actor
            if pid and aid:
                proposal_opinions.append(
                    ProposalOpinionProjection(
                        proposal_id=pid,
                        agent_id=aid,
                        stance=_str_or_none(payload.get("stance")) or "support",
                        opinion=_str_or_none(payload.get("opinion")) or event.message,
                        confidence=_float_or_none(payload.get("confidence")),
                        phase=_str_or_none(payload.get("phase")),
                        source_event_id=event.id,
                    )
                )
        elif event.type == "proposal_challenged":
            critiques.append(
                CritiqueProjection(
                    critic_id=_str_or_none(payload.get("challenger")) or event.actor,
                    target=_str_or_none(payload.get("target")),
                    critique=_str_or_none(payload.get("objection")) or event.message,
                    improvements=[item for item in [_str_or_none(payload.get("suggested_revision"))] if item],
                    source_event_id=event.id,
                )
            )
        elif event.type == "proposal_revised":
            agent_id = _str_or_none(payload.get("agent_id")) or event.actor
            revised = _str_or_none(payload.get("revised_proposal"))
            if agent_id and revised:
                revisions.append(RevisionProjection(agent_id=agent_id, revised_proposal=revised, changes=_list_of_strings(payload.get("changes")), source_event_id=event.id))
        elif event.type == "peer_monitor_report":
            critiques.append(
                CritiqueProjection(
                    critic_id=event.actor,
                    target=_str_or_none(payload.get("reviewed")),
                    critique=_str_or_none(payload.get("critique")) or event.message,
                    risks=_list_of_strings(payload.get("risks")),
                    improvements=_list_of_strings(payload.get("improvements")),
                    source_event_id=event.id,
                )
            )
        elif event.type == "vote_cast":
            choice = _str_or_none(payload.get("choice"))
            if event.actor and choice:
                ballots.append(BallotProjection(voter_id=event.actor, choice=choice, reason=_str_or_none(payload.get("reason")), confidence=_float_or_none(payload.get("confidence")), source_event_id=event.id))
        elif event.type == "ballots_tallied":
            selected_winner = _str_or_none(payload.get("winner")) or selected_winner
        elif event.type == "solution_selected":
            selected_winner = event.actor or selected_winner
        elif event.type == "winner_selected":
            selected_winner = _str_or_none(payload.get("winner_agent_id")) or selected_winner
            selected_proposal_id = _str_or_none(payload.get("winning_proposal_id"))
            winner_rationale = WinnerRationaleProjection(
                winner_agent_id=_str_or_none(payload.get("winner_agent_id")) or event.actor or "",
                winning_proposal_id=_str_or_none(payload.get("winning_proposal_id")),
                why_won=_str_or_none(payload.get("why_won")) or event.message,
                supporting_votes=_list_of_strings(payload.get("supporting_votes")),
                critical_tradeoffs=_list_of_strings(payload.get("critical_tradeoffs")),
                dissent_carried=_list_of_strings(payload.get("dissent_carried")),
            )
        elif event.type == "leader_synthesis":
            leader_synthesis = LeaderSynthesisProjection(
                winner_agent_id=_str_or_none(payload.get("winner_agent_id")) or event.actor or "",
                winning_proposal_id=_str_or_none(payload.get("winning_proposal_id")),
                winning_proposal_summary=_str_or_none(payload.get("winning_proposal_summary")) or event.message,
                why_won=_str_or_none(payload.get("why_won")) or "",
                critical_tradeoffs=_list_of_strings(payload.get("critical_tradeoffs")),
                carried_dissent=_list_of_strings(payload.get("carried_dissent")),
                caveats=_list_of_strings(payload.get("caveats")),
                confidence=_float_or_none(payload.get("confidence")),
                synthesis_kind=_str_or_none(payload.get("synthesis_kind")) or "leader_synthesis",
                source_event_id=event.id,
            )
        elif event.type == "agent_objection_registered":
            objection_text = _str_or_none(payload.get("objection")) or event.message
            if payload.get("blocks_execution"):
                blocking_objections.append(objection_text)
            else:
                non_blocking_dissent.append(objection_text)
            unresolved_dissent.append(objection_text)
        elif event.type in {"working_brief_finalized", "meeting_recap"}:
            unresolved_dissent.extend(_list_of_strings(payload.get("unresolved_dissent")))

    if selected_winner and not selected_proposal_id:
        selected_proposal_id = proposal_ids_by_agent.get(selected_winner)

    # A leader synthesis or winner-selected event is the decision record.  A
    # prior tally is useful history, but cannot leave the page with a second
    # winner (or a "no winner" heading) after the final record exists.
    authoritative_winner = leader_synthesis or winner_rationale
    if authoritative_winner is not None:
        selected_winner = authoritative_winner.winner_agent_id or selected_winner
        selected_proposal_id = authoritative_winner.winning_proposal_id or selected_proposal_id

    required = {
        "proposals": bool(proposals),
        "ballots": bool(ballots),
        "selected_winner": selected_winner is not None,
        "critiques": bool(critiques),
    }
    evidence_status, missing_sources = _evidence(required)
    return DecisionReview(
        task_id=task_id,
        current_phase=_phase(events),
        proposals=proposals,
        proposal_opinions=proposal_opinions,
        critiques=critiques,
        revisions=revisions,
        ballots=ballots,
        selected_winner=selected_winner,
        selected_proposal_id=selected_proposal_id,
        winner_rationale=winner_rationale,
        leader_synthesis=leader_synthesis,
        blocking_objections=[
            item for item in blocking_objections
            if not _claim_resolved_by_validation(item, events)
        ],
        non_blocking_dissent=[item for item in non_blocking_dissent if not _claim_resolved_by_validation(item, events)],
        unresolved_dissent=[
            item for item in unresolved_dissent
            if not _claim_resolved_by_validation(item, events)
        ],
        supporting_artifacts=_artifacts(events),
        evidence_status=evidence_status,
        missing_sources=missing_sources,
    )


def _completion_outcome(events: list[SocietyEvent]) -> str:
    terminal_status = derive_task_status(events)
    if terminal_status in {"failed", "interrupted"}:
        return terminal_status
    has_complete = any(event.type == "task_complete" for event in events)
    has_remediation = any(event.type == "task_remediation" for event in events)
    if has_complete:
        # Acceptance evaluation is emitted from the runtime contract and is
        # therefore more authoritative than prose retained in a proposal or
        # recap.  Preserve explicit failed/remediation outcomes when present.
        for event in reversed(events):
            if event.type != "acceptance_evidence_evaluated":
                continue
            terminal = _str_or_none(_payload(event).get("terminal_status"))
            if terminal in {"failed", "remediation", "complete_with_warnings"}:
                return terminal
        for event in events:
            if event.type == "task_complete":
                acc = _payload(event).get("acceptance_status")
                if acc == "complete_with_warnings":
                    return "complete_with_warnings"
        if _has_caveats(events):
            return "complete_with_caveats"
        return "complete"
    if has_remediation:
        return "remediation"
    if any(event.type in {"run_failed", "task_failed"} for event in events):
        return "failed"
    if any(event.type == "user_clarification_requested" for event in events):
        return "waiting_for_user"
    return "partial"


def _has_caveats(events: list[SocietyEvent]) -> bool:
    """Detect whether a completed run carried unresolved caveats.

    Caveats include: failed validation gate checks, blocked subtask reports,
    unresolved dissent in the working brief or meeting recap, and leader
    synthesis caveats.
    """

    # Only the latest gate describes the current validation state.  Earlier
    # failed attempts are history once a later gate has passed.
    latest_gate = next((event for event in reversed(events) if event.type == "validation_gate_completed"), None)
    if latest_gate is not None:
        latest_payload = _payload(latest_gate)
        failed_checks = latest_payload.get("failed_checks", [])
        unresolved_failed_checks = [
            item for item in failed_checks
            if not _claim_resolved_by_validation(
                str(item.get("reason", "")) if isinstance(item, dict) else str(item), events
            )
        ] if isinstance(failed_checks, list) else []
        if latest_payload.get("passed") is not True and (not failed_checks or unresolved_failed_checks):
            return True

    for event in events:
        payload = _payload(event)
        if event.type == "validation_gate_completed":
            continue
        if event.type == "delegation_reported":
            status = _str_or_none(payload.get("status"))
            if status == "blocked" and not _claim_resolved_by_validation(event.message, events):
                return True
            blockers = _list_of_strings(payload.get("blockers"))
            if any(not _claim_resolved_by_validation(item, events) for item in blockers):
                return True
        if event.type in {"working_brief_finalized", "meeting_recap"}:
            dissent = _list_of_strings(payload.get("unresolved_dissent"))
            if any(not _claim_resolved_by_validation(item, events) for item in dissent):
                return True
        if event.type == "leader_synthesis":
            caveats = _list_of_strings(payload.get("caveats"))
            if any(not _claim_resolved_by_validation(item, events) for item in caveats):
                return True
    return False


def project_recap(task_id: str, events: list[SocietyEvent], task_summary: dict[str, Any] | None = None) -> RunRecap:
    plan_changes: list[str] = []
    dissents: list[str] = []
    lessons: list[str] = []
    trust_reputation_changes: list[dict[str, Any]] = []
    metrics: dict[str, Any] | None = None
    mind_changes: list[dict[str, Any]] = []

    for event in events:
        payload = _payload(event)
        if event.type == "meeting_recap":
            plan_changes.extend(_list_of_strings(payload.get("plan_changes")))
            dissents.extend(_list_of_strings(payload.get("unresolved_dissent")))
            lessons.extend(_list_of_strings(payload.get("saved_lessons")))
        elif event.type == "working_brief_finalized":
            dissents.extend(_list_of_strings(payload.get("unresolved_dissent")))
        elif event.type == "agent_changed_mind":
            mind_changes.append({"actor": event.actor, **payload, "source_event_id": event.id})
        elif event.type in {"trust_updated", "reputation_updated"}:
            trust_reputation_changes.append({"type": event.type, "payload": payload, "source_event_id": event.id})
        elif event.type == "learning_recorded":
            lessons.append(event.message)
        elif event.type == "task_metrics":
            metrics = payload

    required = {
        "task_complete": any(event.type == "task_complete" for event in events),
        "meeting_recap": bool(plan_changes or dissents or lessons),
        "metrics": metrics is not None,
    }
    evidence_status, missing_sources = _evidence(required)
    # Do not replay disproven proof-gap language in Recap.  It remains in the
    # immutable event stream, while this current-state projection reflects the
    # later validator outcome.
    plan_changes = [item for item in plan_changes if not _claim_resolved_by_validation(item, events)]
    dissents = [item for item in dissents if not _claim_resolved_by_validation(item, events)]
    lessons = [item for item in lessons if not _claim_resolved_by_validation(item, events)]
    return RunRecap(
        task_id=task_id,
        status=_task_status(events) if events else str((task_summary or {}).get("status") or "running"),
        duration_seconds=_duration(events),
        turns=sum(1 for event in events if event.type in {"conversation_turn", "agent_goal_opinion", "targeted_question_answered"}),
        events=len(events),
        plan_changes=plan_changes,
        mind_changes=mind_changes,
        dissents=dissents,
        saved_lessons=lessons,
        trust_reputation_changes=trust_reputation_changes,
        delegation_outcomes=_delegation_summary(events),
        completion_outcome=_completion_outcome(events),
        failure=_failure(events),
        metrics=metrics,
        final_answer=_str_or_none((task_summary or {}).get("final_answer")) or _final_answer(events),
        evidence_status=evidence_status,
        missing_sources=missing_sources,
    )


def project_dossier(
    agent: SocietyAgent,
    events: list[SocietyEvent],
    memory_records: list[dict[str, Any]],
    reputation: float | None = None,
    task_id: str | None = None,
) -> AgentDossier:
    trust: list[dict[str, Any]] = []
    run_lessons: list[str] = []
    tendencies: list[str] = []
    published_notes: list[str] = []

    scoped_events = [e for e in events if task_id is None or e.task_id == task_id] if task_id else events
    agent_events = [event for event in scoped_events if event.actor == agent.id or _payload(event).get("agent_id") == agent.id]

    for event in scoped_events:
        payload = _payload(event)
        if event.type == "trust_updated" and payload.get("target_agent_id") == agent.id:
            trust.append({"payload": payload, "source_event_id": event.id})
        if event.type == "learning_recorded":
            run_lessons.append(event.message)
        if event.type == "meeting_recap":
            run_lessons.extend(_list_of_strings(payload.get("saved_lessons")))
        if event.type == "private_note_published" and payload.get("agent_id") == agent.id:
            note_text = _str_or_none(payload.get("note"))
            if note_text:
                published_notes.append(note_text)

    stance_counts = Counter()
    for event in agent_events:
        stance = _str_or_none(_payload(event).get("stance")) or _stance_from_event(event)
        if stance:
            stance_counts[stance] += 1
    tendencies.extend(f"{stance}: {count}" for stance, count in stance_counts.most_common())
    current_default_blockers = [
        item for item in agent.profile.default_blockers
        if not _claim_resolved_by_validation(item, scoped_events)
    ]
    if current_default_blockers:
        tendencies.append("default blockers: " + "; ".join(current_default_blockers))

    recent_stances = [stance for stance in _latest_stances(scoped_events) if stance.agent_id == agent.id]
    this_run_summary: dict[str, Any] = {}
    if task_id:
        this_run_summary = {
            "task_id": task_id,
            "status": _task_status(scoped_events),
            "phase": _phase(scoped_events),
            "agent_contributions": len(agent_events),
            "final_answer": _final_answer(scoped_events),
        }
    this_run_timeline = [
        {"type": e.type, "message": e.message, "actor": e.actor, "created_at": e.created_at, "source_event_id": e.id}
        for e in agent_events
        if not _claim_resolved_by_validation(e.message, scoped_events)
    ]
    all_tool_usage = _tool_usage_summary(scoped_events)
    run_tool_usage = [u for u in all_tool_usage if u.agent_id == agent.id]
    run_delegation = [
        d for d in _delegation_summary(scoped_events)
        if d.agent_id == agent.id or d.assigned_by == agent.id
    ]
    required = {
        "agent_profile": True,
        "behavioral_events": bool(agent_events),
    }
    evidence_status, missing_sources = _evidence(required)
    run_lessons = [item for item in run_lessons if not _claim_resolved_by_validation(item, scoped_events)]
    published_notes = [item for item in published_notes if not _claim_resolved_by_validation(item, scoped_events)]
    return AgentDossier(
        agent=agent,
        task_id=task_id,
        this_run_summary=this_run_summary,
        this_run_timeline=this_run_timeline,
        run_lessons=run_lessons,
        published_notes=published_notes,
        run_tool_usage=run_tool_usage,
        run_delegation=run_delegation,
        reputation=agent.reputation if reputation is None else reputation,
        trust=trust,
        behavioral_tendencies=tendencies,
        recent_stances=recent_stances,
        evidence_status=evidence_status,
        missing_sources=missing_sources,
    )


def _duration(events: list[SocietyEvent]) -> float | None:
    if len(events) < 2:
        return None
    try:
        start = datetime.fromisoformat(events[0].created_at)
        end = datetime.fromisoformat(events[-1].created_at)
    except ValueError:
        return None
    return max((end - start).total_seconds(), 0.0)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item)]
