from __future__ import annotations

from agno.run import RunContext
from agno.tools import tool

from ..schemas.social import (
    AgentPosition,
    EndorsementRecord,
    MindChangeRecord,
    ObjectionRecord,
    PrivateNote,
    TrustUpdate,
)


def _room(run_context: RunContext) -> dict:
    """Return the public social room in session state."""

    return run_context.session_state.setdefault(
        "public_room",
        {
            "positions": [],
            "objections": [],
            "endorsements": [],
            "mind_changes": [],
            "published_private_notes": [],
        },
    )


def _as_list(value: list[str] | str | None) -> list[str]:
    """Normalize list-like tool arguments."""

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]


@tool(name="state_position", stop_after_tool_call=True)
def state_position_tool(
    run_context: RunContext,
    agent_id: str,
    phase: str,
    stance: str,
    reason: str,
    confidence: float = 0.5,
    target: str | None = None,
    conditions: list[str] | str | None = None,
) -> str:
    """Record one agent's public stance."""

    valid = {"support", "oppose", "uncertain", "defer", "block"}
    normalized_stance = stance if stance in valid else "uncertain"
    result = AgentPosition(
        agent_id=agent_id,
        phase=phase,
        stance=normalized_stance,
        target=target,
        reason=reason,
        confidence=confidence,
        conditions=_as_list(conditions),
    )
    _room(run_context).setdefault("positions", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="register_objection", stop_after_tool_call=True)
def register_objection_tool(
    run_context: RunContext,
    agent_id: str,
    severity: str,
    objection: str,
    resolution_condition: str,
    blocks_execution: bool = False,
    target: str | None = None,
) -> str:
    """Record a severity-tagged objection."""

    valid = {"low", "medium", "high", "critical"}
    normalized_severity = severity if severity in valid else "medium"
    result = ObjectionRecord(
        agent_id=agent_id,
        target=target,
        severity=normalized_severity,
        objection=objection,
        resolution_condition=resolution_condition,
        blocks_execution=blocks_execution or normalized_severity == "critical",
    )
    _room(run_context).setdefault("objections", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="endorse_agent", stop_after_tool_call=True)
def endorse_agent_tool(
    run_context: RunContext,
    agent_id: str,
    endorsed_agent_id: str,
    domain: str,
    reason: str,
    confidence: float = 0.5,
) -> str:
    """Record a public endorsement or deferral signal."""

    result = EndorsementRecord(
        agent_id=agent_id,
        endorsed_agent_id=endorsed_agent_id,
        domain=domain,
        reason=reason,
        confidence=confidence,
    )
    _room(run_context).setdefault("endorsements", []).append(result.model_dump())
    run_context.session_state.setdefault("trust_updates", []).append(
        TrustUpdate(
            evaluator_id=agent_id,
            target_agent_id=endorsed_agent_id,
            domain="endorsement",
            delta=min(0.05, max(0.0, confidence * 0.05)),
            reason=f"Endorsed for {domain}: {reason}",
        ).model_dump()
    )
    return result.model_dump_json()


@tool(name="change_mind", stop_after_tool_call=True)
def change_mind_tool(
    run_context: RunContext,
    agent_id: str,
    previous_stance: str,
    new_stance: str,
    reason: str,
    trigger_agent_id: str | None = None,
) -> str:
    """Record a stance change caused by new evidence or another agent."""

    result = MindChangeRecord(
        agent_id=agent_id,
        previous_stance=previous_stance,
        new_stance=new_stance,
        trigger_agent_id=trigger_agent_id,
        reason=reason,
    )
    _room(run_context).setdefault("mind_changes", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="record_private_note", stop_after_tool_call=True)
def record_private_note_tool(
    run_context: RunContext,
    agent_id: str,
    phase: str,
    note: str,
    may_publish: bool = False,
) -> str:
    """Record a private note for one agent."""

    result = PrivateNote(agent_id=agent_id, phase=phase, note=note, may_publish=may_publish)
    private = run_context.session_state.setdefault("private_agent_state", {})
    private.setdefault(agent_id, {}).setdefault("private_notes", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="publish_private_note", stop_after_tool_call=True)
def publish_private_note_tool(
    run_context: RunContext,
    agent_id: str,
    phase: str,
    note: str,
) -> str:
    """Publish a private note into the public room."""

    result = PrivateNote(agent_id=agent_id, phase=phase, note=note, may_publish=True)
    _room(run_context).setdefault("published_private_notes", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="evaluate_peer", stop_after_tool_call=True)
def evaluate_peer_tool(
    run_context: RunContext,
    evaluator_id: str,
    target_agent_id: str,
    domain: str,
    delta: float,
    reason: str,
) -> str:
    """Record a contextual trust signal between agents."""

    result = TrustUpdate(
        evaluator_id=evaluator_id,
        target_agent_id=target_agent_id,
        domain=domain,
        delta=delta,
        reason=reason,
    )
    run_context.session_state.setdefault("trust_updates", []).append(result.model_dump())
    return result.model_dump_json()
