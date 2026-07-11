from __future__ import annotations

import json

from agno.run import RunContext
from agno.tools import tool

from ..schemas.governance import (
    CritiqueReport,
    LeaderDecision,
    LeaderSynthesisRecord,
    SpawnDecision,
    VoteDecision,
)


def _as_list(value: list[str] | str | None) -> list[str]:
    """Normalize model-provided list arguments that arrive as bullet strings."""

    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]


@tool(name="elect_leader", stop_after_tool_call=True)
def elect_leader_tool(leader_id: str, reason: str, confidence: float = 0.8, run_context: RunContext | None = None) -> str:
    """Record the leader election decision.

    Args:
        leader_id: The id of the elected leader agent from the roster.
        reason: Why this agent was chosen as leader.
        confidence: Confidence score from 0.0 to 1.0.

    Returns:
        JSON string with the validated leader decision.
    """
    decision = LeaderDecision(leader_id=leader_id, reason=reason, confidence=confidence)
    if run_context is not None:
        state = run_context.session_state
        state["leader_id"] = leader_id
        state["phase"] = "leader_elected"
    return decision.model_dump_json()


@tool(name="decide_spawn", stop_after_tool_call=True)
def decide_spawn_tool(spawn: bool, reason: str, specialist_role: str | None = None, run_context: RunContext | None = None) -> str:
    """Record the spawn decision for a child specialist agent.

    Args:
        spawn: Whether to spawn a child specialist agent.
        reason: Why spawning or not spawning is the right decision.
        specialist_role: Role description for the specialist if spawning.

    Returns:
        JSON string with the validated spawn decision.
    """
    decision = SpawnDecision(spawn=spawn, specialist_role=specialist_role, reason=reason)
    if run_context is not None:
        state = run_context.session_state
        state["spawn_decision"] = decision.model_dump()
    return decision.model_dump_json()


@tool(name="cast_vote", stop_after_tool_call=True)
def cast_vote_tool(choice: str, reason: str, confidence: float = 0.8, run_context: RunContext | None = None) -> str:
    """Record a vote for the best proposal.

    Args:
        choice: The agent id of the best proposal.
        reason: Why this proposal was chosen.
        confidence: Confidence score from 0.0 to 1.0.

    Returns:
        JSON string with the validated vote decision.
    """
    decision = VoteDecision(choice=choice, reason=reason, confidence=confidence)
    if run_context is not None:
        state = run_context.session_state
        voter_id = state.get("current_actor", "unknown")
        state.setdefault("ballots", []).append({
            "voter": voter_id,
            "choice": choice,
            "reason": reason,
            "confidence": confidence,
        })
    return decision.model_dump_json()


@tool(name="peer_review", stop_after_tool_call=True)
def peer_review_tool(
    critique: str,
    risks: list[str] | str,
    improvements: list[str] | str,
    confidence: float = 0.8,
    run_context: RunContext | None = None,
) -> str:
    """Record a critical review of the winning solution.

    Args:
        critique: Critical review of the winning solution.
        risks: Identified risks and unsupported assumptions.
        improvements: Suggested improvements.
        confidence: Confidence score from 0.0 to 1.0.

    Returns:
        JSON string with the validated critique report.
    """
    report = CritiqueReport(critique=critique, risks=_as_list(risks), improvements=_as_list(improvements), confidence=confidence)
    if run_context is not None:
        state = run_context.session_state
        state["critique"] = report.model_dump()
    return report.model_dump_json()


@tool(name="leader_synthesize", stop_after_tool_call=True)
def leader_synthesize_tool(
    winner_agent_id: str,
    winning_proposal_summary: str,
    why_won: str,
    critical_tradeoffs: list[str] | str | None = None,
    carried_dissent: list[str] | str | None = None,
    caveats: list[str] | str | None = None,
    winning_proposal_id: str | None = None,
    confidence: float = 0.7,
    run_context: RunContext | None = None,
) -> str:
    """Record the leader's explicit synthesis of the decision.

    This is a first-class truth artifact separate from plain ballot tallying.
    It expresses: winning proposal or winner, why it won, critical tradeoffs,
    carried dissent/caveats, and that this is the leader synthesis.

    Args:
        winner_agent_id: The agent id of the winner.
        winning_proposal_summary: Human-readable summary of the winning proposal.
        why_won: Why this proposal won.
        critical_tradeoffs: Key tradeoffs accepted by the decision.
        carried_dissent: Dissent or caveats carried forward.
        caveats: Additional caveats from the leader.
        winning_proposal_id: Stable id of the winning proposal.
        confidence: Confidence in the synthesis.

    Returns:
        JSON string with the validated leader synthesis record.
    """
    record = LeaderSynthesisRecord(
        winner_agent_id=winner_agent_id,
        winning_proposal_id=winning_proposal_id,
        winning_proposal_summary=winning_proposal_summary,
        why_won=why_won,
        critical_tradeoffs=_as_list(critical_tradeoffs),
        carried_dissent=_as_list(carried_dissent),
        caveats=_as_list(caveats),
        confidence=confidence,
    )
    if run_context is not None:
        run_context.session_state["leader_synthesis"] = record.model_dump()
    return record.model_dump_json()
