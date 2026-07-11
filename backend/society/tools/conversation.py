from __future__ import annotations

import json

from agno.run import RunContext
from agno.tools import tool

from ..schemas.conversation import GoalDiscussionStatement, ReadinessBallot


def _as_list(value: list[str] | str | None) -> list[str]:
    """Normalize list arguments because smaller models often send bullet strings."""

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    cleaned = value.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned.strip("[]")
    return [
        line.strip(" -\t\"'")
        for line in cleaned.replace("\\n", "\n").splitlines()
        if line.strip(" -\t\"',")
    ] or [cleaned]


@tool(name="submit_goal_discussion", stop_after_tool_call=True)
def submit_goal_discussion_tool(
    run_context: RunContext,
    round: int,
    agent_id: str,
    responds_to: str | None = None,
    stance: str = "clarifies",
    interpretation: str = "",
    unique_contribution: str = "",
    success_criteria: list[str] | str | None = None,
    concerns: list[str] | str | None = None,
    suggested_scope: str = "",
    question_for_next: str | None = None,
    spoken_turn: str = "",
) -> str:
    """Record one agent's conversational contribution before execution."""

    valid_stances = {"builds_on", "challenges", "clarifies", "blocks"}
    normalized_stance = stance if stance in valid_stances else "clarifies"

    result = GoalDiscussionStatement(
        round=round,
        agent_id=agent_id,
        responds_to=responds_to,
        stance=normalized_stance,
        interpretation=interpretation,
        unique_contribution=unique_contribution,
        success_criteria=_as_list(success_criteria),
        concerns=_as_list(concerns),
        suggested_scope=suggested_scope,
        question_for_next=question_for_next,
        spoken_turn=spoken_turn,
    )
    run_context.session_state.setdefault("goal_discussions", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="cast_readiness_vote", stop_after_tool_call=True)
def cast_readiness_vote_tool(
    run_context: RunContext,
    attempt: int,
    agent_id: str,
    ready: bool,
    critical_blocker: bool,
    reason: str,
    required_clarification: str | None = None,
) -> str:
    """Record one agent's readiness vote before execution starts."""

    result = ReadinessBallot(
        attempt=attempt,
        agent_id=agent_id,
        ready=ready,
        critical_blocker=critical_blocker,
        reason=reason,
        required_clarification=required_clarification,
    )
    run_context.session_state.setdefault("readiness_ballots", []).append(result.model_dump())
    return result.model_dump_json()
