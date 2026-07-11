from __future__ import annotations

from .orchestrator import SocietyOrchestrator
from .schemas.social import (
    AgentPosition,
    CollaborationAction,
    EndorsementRecord,
    MindChangeRecord,
    ObjectionRecord,
    PrivateNote,
    TrustUpdate,
)


REQUIRED_EVENT_TYPES = {
    "agent_position_stated",
    "agent_objection_registered",
    "agent_endorsed_peer",
    "agent_changed_mind",
    "private_note_published",
    "agent_help_requested",
    "agent_deferred_ownership",
    "agent_joined_coalition",
    "trust_updated",
    "artifact_section_critiqued",
    "shared_artifact_revised",
    "personality_drifted",
    "failure_recovery_attempted",
}


def run_behavior_preflight() -> dict[str, object]:
    """Verify the human-behavior trace and learning primitives without an LLM."""

    orchestrator = SocietyOrchestrator()
    task_id = "behavior-preflight"
    emitted: list[str] = []
    orchestrator._state(task_id)
    orchestrator._emit = lambda task_id, type, message, actor=None, payload=None: emitted.append(type)  # type: ignore[method-assign]

    orchestrator._record_position(
        task_id,
        AgentPosition(
            agent_id="ada",
            phase="working_brief",
            stance="support",
            reason="Scope and ownership are clear enough to proceed.",
            confidence=0.8,
        ),
    )
    orchestrator._record_objection(
        task_id,
        ObjectionRecord(
            agent_id="critic",
            severity="high",
            objection="The acceptance gate is not explicit.",
            resolution_condition="Add a smoke test before delivery.",
        ),
    )
    orchestrator._record_endorsement(
        task_id,
        EndorsementRecord(
            agent_id="researcher",
            endorsed_agent_id="ada",
            domain="system shape",
            reason="Ada has the clearest decomposition.",
            confidence=0.75,
        ),
    )
    orchestrator._record_mind_change(
        task_id,
        MindChangeRecord(
            agent_id="builder",
            previous_stance="defend_original_proposal",
            new_stance="revised_after_challenge",
            trigger_agent_id="critic",
            reason="The critic identified a missing validation gate.",
        ),
    )
    orchestrator._record_private_note(
        task_id,
        PrivateNote(
            agent_id="researcher",
            phase="working_brief",
            note="Privately verify evidence quality before implementation.",
            may_publish=False,
        ),
    )
    orchestrator._publish_private_note(
        task_id,
        PrivateNote(
            agent_id="critic",
            phase="working_brief",
            note="The validation gate should be visible to the team.",
            may_publish=True,
        ),
    )
    orchestrator._record_collaboration_action(
        task_id,
        CollaborationAction(
            agent_id="builder",
            action="help_requested",
            target_agent_id="ada",
            phase="subtask_assignment",
            reason="Builder asks Ada to keep scope bounded.",
            confidence=0.7,
        ),
    )
    orchestrator._record_collaboration_action(
        task_id,
        CollaborationAction(
            agent_id="researcher",
            action="ownership_deferred",
            target_agent_id="ada",
            phase="leader_election",
            reason="Researcher defers coordination to Ada.",
            confidence=0.8,
        ),
    )
    orchestrator._record_collaboration_action(
        task_id,
        CollaborationAction(
            agent_id="critic",
            action="coalition_joined",
            target_agent_id="builder",
            phase="vote",
            reason="Critic backs the proposal after validation is added.",
            confidence=0.8,
        ),
    )
    orchestrator._record_trust_update(
        task_id,
        TrustUpdate(
            evaluator_id="society",
            target_agent_id="builder",
            domain="delivery",
            delta=0.05,
            reason="Builder accepted a validation-driven revision.",
        ),
    )
    orchestrator._record_shared_artifact_revision(
        task_id,
        artifact_id="artifact-preflight",
        section="plan.validation",
        editor_id="builder",
        reviewer_id="critic",
        change_summary="Added a visible smoke test to the shared plan.",
        critique="The original shared plan did not say how success would be measured.",
    )
    orchestrator._apply_personality_drift(
        task_id,
        orchestrator.agents["critic"],
        [{"signal": "raised_blocker", "lesson": "The blocker changed the plan."}],
        {"critique": 0.05},
    )
    state = orchestrator._state(task_id)
    state["roster"] = [{"id": "architect"}, {"id": "critic"}]
    state["phase"] = "preflight"
    state["current_actor"] = "critic"
    orchestrator._record_failure_recovery(task_id, "preflight simulated failure", state)

    missing = REQUIRED_EVENT_TYPES.difference(emitted)
    if missing:
        raise RuntimeError(f"behavior_preflight missing events: {sorted(missing)}")

    counts = orchestrator._social_trace_counts(state)
    if counts["collaboration_actions"] != 3 or counts["mind_changes"] != 1:
        raise RuntimeError(f"behavior_preflight unexpected counts: {counts}")

    critic_lessons = orchestrator._social_lessons_for_agent(state, "critic", "builder")
    builder_lessons = orchestrator._social_lessons_for_agent(state, "builder", "builder")
    critic_signals = {lesson["signal"] for lesson in critic_lessons}
    builder_signals = {lesson["signal"] for lesson in builder_lessons}
    if not {"raised_blocker", "persuaded_teammate", "coalition_joined"}.issubset(critic_signals):
        raise RuntimeError(f"behavior_preflight missing critic lessons: {critic_lessons}")
    if not {"changed_mind", "help_requested", "proposal_attracted_support"}.issubset(builder_signals):
        raise RuntimeError(f"behavior_preflight missing builder lessons: {builder_lessons}")

    return {
        "events": sorted(REQUIRED_EVENT_TYPES),
        "counts": counts,
        "critic_signals": sorted(critic_signals),
        "builder_signals": sorted(builder_signals),
    }


def main() -> None:
    result = run_behavior_preflight()
    print(
        "behavior_preflight=passed "
        f"events={len(result['events'])} "
        f"collaboration_actions={result['counts']['collaboration_actions']}"
    )


if __name__ == "__main__":
    main()
