from __future__ import annotations

from typing import Any

from agno.team import Team
from agno.team.mode import TeamMode

from .agents import build_agno_agent, build_model
from .models import SocietyAgent
from .schemas.coordination import TeamCoordinationBrief

from config import Settings


def build_society_team(
    members: list[SocietyAgent],
    settings: Settings,
    instructions: list[str] | None = None,
    session_state: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> Team:
    """Build an Agno Team configured for governance coordination.

    Uses TeamMode.coordinate so the team leader delegates to members,
    members report back, and the leader synthesizes. The team is configured
    with ``output_schema=TeamCoordinationBrief`` so the coordination pass
    returns a typed, truth-useful artifact the orchestrator can consume
    directly for delegation and observable backend truth.
    """

    agno_members = [
        build_agno_agent(
            identity=member,
            settings=settings,
        )
        for member in members
    ]

    default_instructions = [
        "You are a governance team solving a task collaboratively.",
        "Follow this order: elect a leader, gather proposals, debate, vote, critique.",
        "Use governance tools to record decisions in session_state.",
        "The leader coordinates; other members contribute domain expertise.",
        "Produce a structured coordination brief with summary, proposed_subtasks, "
        "open_questions, recommended_focus, confidence, risk_signals, evidence_gaps, "
        "assumptions, and delegation_hints.",
        "Each proposed_subtask must include agent_id, subtask, why_assigned, "
        "done_criteria, and blocking_if_missing.",
    ]

    kwargs: dict[str, Any] = dict(
        name="Qwendom Society",
        mode=TeamMode.coordinate,
        model=build_model(settings),
        members=agno_members,
        instructions=instructions or default_instructions,
        output_schema=TeamCoordinationBrief,
        markdown=True,
        show_members_responses=True,
        max_iterations=4,
        determine_input_for_members=False,
        delegate_to_all_members=False,
    )

    if session_state is not None:
        kwargs["session_state"] = session_state
    if session_id is not None:
        kwargs["session_id"] = session_id

    return Team(**kwargs)
