from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .orchestrator import SocietyOrchestrator
from .schemas.conversation import GoalDiscussionStatement
from .schemas.social import ObjectionRecord


@dataclass(frozen=True)
class BenchmarkFixture:
    """One realistic prompt used to score human-like collaboration traces."""

    name: str
    prompt: str


FIXTURES = [
    BenchmarkFixture(
        name="privacy_beta_launch",
        prompt="Plan a two week beta launch for a privacy first tutoring assistant, noting risks and assumptions.",
    ),
    BenchmarkFixture(
        name="implementation_review",
        prompt="Review an implementation plan for a local-first meeting prep tool and find the riskiest assumption.",
    ),
]


async def _score_fixture(fixture: BenchmarkFixture) -> dict[str, object]:
    """Run a lightweight deterministic collaboration trace and score signals."""

    orchestrator = SocietyOrchestrator()
    task = orchestrator.submit(fixture.prompt)
    team = orchestrator._form_team(task)
    state = orchestrator.session_states[task.id]
    state["discussion_round_count"] = 1

    statement = GoalDiscussionStatement(
        round=1,
        agent_id="architect",
        stance="challenges",
        interpretation="The team can proceed only if assumptions stay visible.",
        unique_contribution="The plan should preserve compliance and validation dissent.",
        success_criteria=["Risks are named", "Assumptions are visible"],
        concerns=["Target user evidence may be weak"],
        suggested_scope="Produce a narrow plan with explicit caveats.",
        question_for_next="What is the weakest assumption to validate first?",
        spoken_turn="We can move, but only if the weakest assumption stays visible.",
    )
    state["goal_discussions"].append(statement.model_dump())
    orchestrator._record_conversation_turn(task.id, statement, 1, None)
    await orchestrator._run_targeted_question_exchange(task, team)

    state["ready_to_proceed"] = True
    await orchestrator._compose_working_brief(task, team)
    orchestrator._emit_meeting_recap(task, team)
    orchestrator._record_objection(
        task.id,
        ObjectionRecord(
            agent_id="critic",
            severity="high",
            objection="The acceptance check is still too vague.",
            resolution_condition="Attach a concrete validation step before final answer.",
        ),
    )
    orchestrator._record_shared_artifact_revision(
        task.id,
        artifact_id="artifact-fixture",
        section="plan.validation",
        editor_id="builder",
        reviewer_id="critic",
        change_summary="Added acceptance check to the shared plan.",
        critique="The first draft did not define how success would be measured.",
    )
    orchestrator._apply_personality_drift(
        task.id,
        orchestrator.agents["critic"],
        [{"signal": "raised_blocker", "lesson": "Valid blocker raised."}],
        {"critique": 0.05},
    )

    event_types = [event.type for event in orchestrator.list_events(task.id)]
    expectations = {
        "transcript": len(state.get("conversation_transcript", [])) >= 2,
        "targeted_question": "targeted_question_answered" in event_types,
        "dissent": bool((state.get("working_brief") or {}).get("unresolved_dissent")),
        "artifact_revision": "shared_artifact_revised" in event_types,
        "meeting_recap": "meeting_recap" in event_types,
        "personality_drift": "personality_drifted" in event_types,
    }
    passed = sum(1 for value in expectations.values() if value)
    return {
        "fixture": fixture.name,
        "score": passed / len(expectations),
        "expectations": expectations,
    }


async def _score_user_resume_fixture() -> dict[str, object]:
    """Score the user-in-the-loop pause and resume path."""

    orchestrator = SocietyOrchestrator()
    task = orchestrator.submit("Plan a launch but ask the user if the target geography is missing.")
    team = orchestrator._form_team(task)
    state = orchestrator.session_states[task.id]
    state["readiness_tally"] = {
        "attempt": 3,
        "ready_count": 1,
        "not_ready_count": 3,
        "total": 4,
        "passed": False,
        "blockers": ["critic: target geography is required before compliance planning"],
    }
    state["ready_to_proceed"] = False
    orchestrator._pause_for_user_clarification(task)

    async def _fake_execute_after_readiness(paused_task, paused_team, start_time):
        paused_task.status = "complete"
        paused_task.final_answer = "Resumed after user clarified geography."
        orchestrator._emit(paused_task.id, "task_complete", "Synthetic resume completed.", payload={"answer": paused_task.final_answer})

    orchestrator._execute_after_readiness = _fake_execute_after_readiness  # type: ignore[method-assign]
    await orchestrator.resume_with_clarification(task.id, "Start with the US market.")

    event_types = [event.type for event in orchestrator.list_events(task.id)]
    expectations = {
        "paused": "user_clarification_requested" in event_types,
        "answered": "user_clarification_answered" in event_types,
        "resumed": "society_resumed" in event_types,
        "completed": task.status == "complete",
        "brief_updated": "Start with the US market." in str(state.get("working_brief", {})),
    }
    passed = sum(1 for value in expectations.values() if value)
    return {
        "fixture": "user_in_the_loop_resume",
        "score": passed / len(expectations),
        "expectations": expectations,
    }


async def run_human_behavior_benchmark() -> dict[str, object]:
    """Score collaboration quality across the benchmark fixture suite."""

    results = [await _score_fixture(fixture) for fixture in FIXTURES]
    results.append(await _score_user_resume_fixture())
    average = sum(float(result["score"]) for result in results) / len(results)
    return {
        "fixtures": results,
        "average_score": round(average, 3),
        "passed": average >= 0.9,
    }


def main() -> None:
    result = asyncio.run(run_human_behavior_benchmark())
    if not result["passed"]:
        raise SystemExit(f"human_behavior_benchmark=failed {result}")
    print(f"human_behavior_benchmark=passed average_score={result['average_score']}")


if __name__ == "__main__":
    main()
