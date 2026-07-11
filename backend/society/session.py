from __future__ import annotations

from typing import Any


def initial_session_state(task_prompt: str, roster: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a fresh session_state dict for a governance run.

    All values are JSON-serializable primitives so Agno can persist them
    through its db layer without custom serializers.
    """

    return {
        "task_prompt": task_prompt,
        "roster": roster,
        "phase": "forming",
        "leader_id": None,
        "proposals": {},
        "proposal_id_map": {},
        "proposal_opinions": [],
        "leader_synthesis": None,
        "challenges": [],
        "revisions": {},
        "ballots": [],
        "tally": {},
        "winner_id": None,
        "critique": None,
        "spawn_decision": None,
        "child_agents": [],
        "subtasks": [],
        "team_coordination_brief": None,
        "task_class": None,
        "workflow_routing": {},
        "artifacts": [],
        "artifact_index": {},
        "shared_artifact_revisions": [],
        "acceptance_checks": [],
        "failed_checks": [],
        "final_deliverable": None,
        "evaluation_metrics": [],
        "reputations": {},
        "goal_discussions": [],
        "conversation_transcript": [],
        "targeted_question_exchanges": [],
        "readiness_ballots": [],
        "readiness_tally": {},
        "ready_to_proceed": None,
        "user_clarification": None,
        "resume_phase": None,
        "meeting_recap": None,
        "discussion_round_count": 0,
        "readiness_attempt_count": 0,
        "working_brief": None,
        "public_room": {
            "positions": [],
            "objections": [],
            "endorsements": [],
            "mind_changes": [],
            "published_private_notes": [],
            "collaboration_actions": [],
        },
        "private_agent_state": {},
        "trust_updates": [],
        "personality_drift": [],
        "failure_recovery": [],
        "metrics": {
            "tool_calls": 0,
            "tool_calls_failed": 0,
            "governance_rounds": 0,
            "debate_rounds": 0,
            "memory_writes": 0,
        },
    }
