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


def _is_substantive_question(question: str) -> bool:
    """Keep readiness questions tied to a decision, fact, risk, or test.

    The tool cannot know the live roster, but it can prevent ceremonial
    "shall we continue" handoffs from entering durable event replay. Invalid
    questions are omitted rather than converted into fabricated debate.
    """

    normalized = " ".join(question.lower().split())
    if len(normalized) < 12:
        return False
    procedural_markers = (
        "are we ready",
        "should we proceed",
        "can we proceed",
        "shall we continue",
        "should we continue",
        "who goes next",
        "any questions",
    )
    if any(marker in normalized for marker in procedural_markers):
        return False
    substantive_markers = (
        "risk",
        "missing",
        "unknown",
        "evidence",
        "fact",
        "conflict",
        "contradict",
        "tradeoff",
        "test",
        "verify",
        "acceptance",
        "criterion",
        "decide",
        "decision",
        "should ",
        "whether ",
        "which ",
    )
    return "?" in normalized and any(marker in normalized for marker in substantive_markers)


@tool(name="submit_goal_discussion", stop_after_tool_call=True)
def submit_goal_discussion_tool(
    run_context: RunContext,
    agent_id: str,
    round: int = 1,
    responds_to: str | None = None,
    stance: str = "clarifies",
    interpretation: str = "",
    unique_contribution: str = "",
    success_criteria: list[str] | str | None = None,
    concerns: list[str] | str | None = None,
    suggested_scope: str = "",
    question_for_next: str | None = None,
    question_target_agent_id: str | None = None,
    spoken_turn: str = "",
    ready: bool = True,
    critical_blocker: bool = False,
    blocker_category: str | None = None,
    required_clarification: str | None = None,
    blocker_owner: str = "",
    blocker_remediation: str = "",
) -> str:
    """Record one planning contribution and its truthful readiness decision."""

    valid_stances = {"builds_on", "challenges", "clarifies", "blocks"}
    normalized_stance = stance if stance in valid_stances else "clarifies"
    valid_categories = {
        "missing_user_input",
        "missing_system_capability",
        "safety_or_policy",
        "future_work",
        "risk",
    }
    normalized_category = blocker_category if blocker_category in valid_categories else None
    normalized_question = question_for_next.strip() if isinstance(question_for_next, str) else None
    normalized_target = question_target_agent_id.strip() if isinstance(question_target_agent_id, str) else None
    if not normalized_question or not normalized_target or not _is_substantive_question(normalized_question):
        normalized_question = None
        normalized_target = None

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
        question_for_next=normalized_question,
        question_target_agent_id=normalized_target,
        spoken_turn=spoken_turn,
        ready=ready,
        critical_blocker=critical_blocker,
        blocker_category=normalized_category,
        required_clarification=required_clarification,
        blocker_owner=blocker_owner,
        blocker_remediation=blocker_remediation,
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
    blocker_category: str | None = None,
    owner: str = "",
    phase: str = "pre_execution",
    remediation: str = "",
) -> str:
    """Record one agent's readiness vote before execution starts."""

    valid_categories = {
        "missing_user_input",
        "missing_system_capability",
        "safety_or_policy",
        "future_work",
        "risk",
    }
    normalized_category = blocker_category if blocker_category in valid_categories else None

    result = ReadinessBallot(
        attempt=attempt,
        agent_id=agent_id,
        ready=ready,
        critical_blocker=critical_blocker,
        reason=reason,
        required_clarification=required_clarification,
        blocker_category=normalized_category,
        owner=owner,
        phase=phase,
        remediation=remediation,
    )
    run_context.session_state.setdefault("readiness_ballots", []).append(result.model_dump())
    return result.model_dump_json()
