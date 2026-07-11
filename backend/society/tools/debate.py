from __future__ import annotations

import json
from uuid import uuid4

from agno.run import RunContext
from agno.tools import tool

from ..schemas.debate import ProposalOpinionRecord


@tool(name="propose", stop_after_tool_call=True)
def propose_tool(run_context: RunContext, agent_id: str, proposal: str, rationale: str) -> str:
    """Submit a governance proposal into the shared session state.

    Args:
        agent_id: The id of the agent submitting the proposal.
        proposal: The proposal text.
        rationale: Why this proposal addresses the task.

    Returns:
        JSON confirmation the proposal was recorded.
    """
    state = run_context.session_state
    proposal_id = f"prop-{agent_id}-{uuid4().hex[:8]}"
    state["proposals"][agent_id] = {
        "proposal_id": proposal_id,
        "proposal": proposal,
        "rationale": rationale,
        "round": state["metrics"]["debate_rounds"],
    }
    state.setdefault("proposal_id_map", {})[agent_id] = proposal_id
    return json.dumps({
        "status": "recorded",
        "proposal_id": proposal_id,
        "agent_id": agent_id,
        "proposal": proposal,
        "rationale": rationale,
        "round": state["metrics"]["debate_rounds"],
    })


@tool(name="challenge", stop_after_tool_call=True)
def challenge_tool(
    run_context: RunContext,
    challenger_id: str,
    target_agent: str,
    objection: str,
    suggested_revision: str,
) -> str:
    """Challenge an existing proposal in the shared session state.

    Args:
        challenger_id: The id of the challenging agent.
        target_agent: The id of the agent whose proposal is challenged.
        objection: What is wrong with the proposal.
        suggested_revision: How the proposal should be improved.

    Returns:
        JSON confirmation the challenge was recorded.
    """
    state = run_context.session_state
    challenge = {
        "challenger": challenger_id,
        "target": target_agent,
        "objection": objection,
        "suggested_revision": suggested_revision,
        "round": state["metrics"]["debate_rounds"],
    }
    if not any(
        item.get("challenger") == challenger_id
        and item.get("target") == target_agent
        and item.get("objection") == objection
        for item in state["challenges"]
    ):
        state["challenges"].append(challenge)
    return json.dumps({"status": "challenge_recorded", **challenge})


@tool(name="revise", stop_after_tool_call=True)
def revise_tool(
    run_context: RunContext,
    agent_id: str,
    revised_proposal: str,
    changes: list[str],
) -> str:
    """Revise a proposal in response to challenges.

    Args:
        agent_id: The id of the agent revising its proposal.
        revised_proposal: The updated proposal text.
        changes: List of changes made in response to challenges.

    Returns:
        JSON confirmation the revision was recorded.
    """
    state = run_context.session_state
    state["revisions"][agent_id] = {
        "revised_proposal": revised_proposal,
        "changes": changes,
        "round": state["metrics"]["debate_rounds"],
    }
    return json.dumps({
        "status": "revision_recorded",
        "agent_id": agent_id,
        "revised_proposal": revised_proposal,
        "changes": changes,
        "round": state["metrics"]["debate_rounds"],
    })


@tool(name="record_proposal_opinion", stop_after_tool_call=True)
def record_proposal_opinion_tool(
    run_context: RunContext,
    agent_id: str,
    proposal_id: str,
    opinion: str,
    stance: str = "neutral",
    confidence: float = 0.5,
    phase: str = "proposal_review",
) -> str:
    """Record one agent's opinion about a specific proposal before voting.

    Args:
        agent_id: The id of the agent recording the opinion.
        proposal_id: The id of the proposal being evaluated.
        opinion: The agent's opinion text about the proposal.
        stance: One of support, oppose, uncertain, neutral.
        confidence: Confidence score from 0.0 to 1.0.
        phase: The phase during which the opinion is recorded.

    Returns:
        JSON string with the validated opinion record.
    """
    valid_stances = {"support", "oppose", "uncertain", "neutral"}
    if stance not in valid_stances:
        stance = "neutral"
    result = ProposalOpinionRecord(
        agent_id=agent_id,
        proposal_id=proposal_id,
        opinion=opinion,
        stance=stance,
        confidence=confidence,
        phase=phase,
    )
    run_context.session_state.setdefault("proposal_opinions", []).append(result.model_dump())
    return result.model_dump_json()
