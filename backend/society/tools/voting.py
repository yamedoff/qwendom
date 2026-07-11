from __future__ import annotations

import json
from collections import Counter

from agno.run import RunContext
from agno.tools import tool

from ..schemas.governance import VoteDecision


@tool(name="cast_ballot", stop_after_tool_call=True)
def cast_ballot_tool(
    run_context: RunContext,
    voter_id: str,
    choice: str,
    reason: str,
    confidence: float = 0.8,
) -> str:
    """Cast a ballot for a proposal in the shared session state.

    Args:
        voter_id: The id of the voting agent.
        choice: The agent id of the preferred proposal.
        reason: Why this proposal was chosen.
        confidence: Confidence score from 0.0 to 1.0.

    Returns:
        JSON string with the validated vote decision.
    """
    ballots = run_context.session_state["ballots"]
    if not any(ballot.get("voter") == voter_id for ballot in ballots):
        ballots.append({
            "voter": voter_id,
            "choice": choice,
            "reason": reason,
            "confidence": confidence,
        })
    return VoteDecision(choice=choice, reason=reason, confidence=confidence).model_dump_json()


@tool(name="tally_ballots", stop_after_tool_call=True)
def tally_ballots_tool(run_context: RunContext) -> str:
    """Tally all ballots in session state and determine the winner.

    Returns:
        JSON with the tally and winner agent id.
    """
    ballots = run_context.session_state["ballots"]
    votes = Counter(b["choice"] for b in ballots)
    tally = dict(votes.most_common())
    winner = votes.most_common(1)[0][0] if votes else None
    run_context.session_state["tally"] = tally
    run_context.session_state["winner_id"] = winner
    run_context.session_state["phase"] = "voted"
    return json.dumps({"tally": tally, "winner": winner})
