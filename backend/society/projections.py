from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import SocietyAgent, SocietyEvent

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
    trace_status: str = "live"
    generated_at: str | None = None
    stale_reason: str | None = None
    failure: FailureProjection | None = None
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
    status = fallback
    for event in events:
        if event.type == "task_complete":
            status = "complete"
        elif event.type in {"task_failed", "run_failed"} and status != "complete":
            status = "failed"
        elif event.type == "user_clarification_requested" and status not in {"complete", "failed"}:
            status = "waiting_for_user"
        elif event.type == "society_resumed" and status == "waiting_for_user":
            status = "running"
    return status


def _phase(events: list[SocietyEvent]) -> str:
    phase = "setup"
    for event in events:
        payload = _payload(event)
        if event.type == "task_complete":
            phase = "complete"
        elif event.type in {"task_failed", "run_failed"}:
            phase = str(payload.get("phase") or "failed")
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
    return blockers


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


def _trace_status(events: list[SocietyEvent], status: str) -> str:
    if any(event.type in {"task_failed", "run_failed"} for event in events):
        return "failed"
    if status == "complete":
        return "complete"
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
    status = str((task_summary or {}).get("status") or _task_status(events))
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
        trace_status=_trace_status(events, status),
        generated_at=datetime.utcnow().isoformat(),
        stale_reason=None,
        failure=_failure(events),
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
        blocking_objections=blocking_objections,
        non_blocking_dissent=non_blocking_dissent,
        unresolved_dissent=unresolved_dissent,
        supporting_artifacts=_artifacts(events),
        evidence_status=evidence_status,
        missing_sources=missing_sources,
    )


def _completion_outcome(events: list[SocietyEvent]) -> str:
    has_complete = any(event.type == "task_complete" for event in events)
    if has_complete:
        if _has_caveats(events):
            return "complete_with_caveats"
        return "complete"
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

    for event in events:
        payload = _payload(event)
        if event.type == "validation_gate_completed" and not payload.get("passed", True):
            return True
        if event.type == "validation_gate_completed":
            failed_checks = payload.get("failed_checks", [])
            if isinstance(failed_checks, list) and failed_checks:
                return True
        if event.type == "delegation_reported":
            status = _str_or_none(payload.get("status"))
            if status == "blocked":
                return True
            blockers = _list_of_strings(payload.get("blockers"))
            if blockers:
                return True
        if event.type in {"working_brief_finalized", "meeting_recap"}:
            dissent = _list_of_strings(payload.get("unresolved_dissent"))
            if dissent:
                return True
        if event.type == "leader_synthesis":
            caveats = _list_of_strings(payload.get("caveats"))
            if caveats:
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
    return RunRecap(
        task_id=task_id,
        status=str((task_summary or {}).get("status") or _task_status(events)),
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
    if agent.profile.default_blockers:
        tendencies.append("default blockers: " + "; ".join(agent.profile.default_blockers))

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
