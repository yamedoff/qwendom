from __future__ import annotations

import time
from typing import Any

from agno.workflow import Step, StepInput, StepOutput, Workflow

from .schemas.evaluation import TaskMetrics


GOVERNANCE_PHASES = [
    "form_and_elect",
    "pre_execution_conversation",
    "spawn_or_delegate",
    "debate",
    "vote",
    "monitor",
    "learn_and_measure",
]

TASK_CLASSES = {"research", "planning", "implementation", "review"}

MAX_REVISION_LOOPS = 2


def classify_task(prompt: str) -> str:
    """Classify a task for bounded adaptive workflow metadata."""

    lowered = prompt.lower()
    if any(word in lowered for word in ("research", "compare", "investigate", "evidence", "source")):
        return "research"
    if any(word in lowered for word in ("implement", "build", "code", "integrate", "prototype")):
        return "implementation"
    if any(word in lowered for word in ("review", "audit", "critique", "validate", "risk")):
        return "review"
    return "planning"


def build_routing_metadata(
    prompt: str,
    routing_enabled: bool = False,
) -> dict[str, Any]:
    """Build bounded routing/condition/loop metadata for the workflow.

    When routing_enabled is False, returns linear metadata only.
    When True, adds condition and loop hints the orchestrator can consume.
    """

    task_class = classify_task(prompt)
    routing: dict[str, Any] = {
        "mode": "linear",
        "task_class": task_class,
    }
    if not routing_enabled:
        return routing

    routing["mode"] = "adaptive"
    routing["conditions"] = {
        "skip_debate_if_single_proposal": True,
        "skip_revision_if_no_challenges": True,
    }
    routing["loops"] = {
        "max_revision_rounds": MAX_REVISION_LOOPS,
        "max_validation_rounds": MAX_REVISION_LOOPS,
    }
    class_hints: dict[str, list[str]] = {
        "research": ["debate", "monitor"],
        "implementation": ["spawn_or_delegate", "debate", "vote", "monitor"],
        "review": ["debate", "monitor"],
        "planning": ["spawn_or_delegate", "debate", "vote"],
    }
    routing["priority_phases"] = class_hints.get(task_class, GOVERNANCE_PHASES)
    return routing


def should_skip_phase(session_state: dict[str, Any], phase: str) -> bool:
    """Check routing metadata to decide if a phase can be safely skipped."""

    routing = session_state.get("workflow_routing", {})
    if routing.get("mode") != "adaptive":
        return False
    conditions = routing.get("conditions", {})
    if phase == "debate" and conditions.get("skip_debate_if_single_proposal"):
        proposals = session_state.get("proposals", {})
        if len(proposals) <= 1:
            return True
    if phase == "revision" and conditions.get("skip_revision_if_no_challenges"):
        challenges = session_state.get("challenges", [])
        if not challenges:
            return True
    return False


def get_loop_budget(session_state: dict[str, Any]) -> int:
    """Return the maximum revision/validation loop iterations allowed."""

    routing = session_state.get("workflow_routing", {})
    if routing.get("mode") != "adaptive":
        return 1
    loops = routing.get("loops", {})
    return int(loops.get("max_revision_rounds", MAX_REVISION_LOOPS))


def _checkpoint_executor_for(phase: str):
    """Record that a governance checkpoint executed inside Agno Workflow."""

    def _checkpoint_executor(step_input: StepInput) -> StepOutput:
        state = step_input.workflow_session.session_data.setdefault("session_state", {})
        checkpoints = state.setdefault("workflow_checkpoints", [])
        checkpoints.append({"phase": phase, "status": "entered"})
        state["phase"] = phase
        return StepOutput(content={"phase": phase, "checkpoint": len(checkpoints)})

    _checkpoint_executor.__name__ = f"{phase}_checkpoint"
    return _checkpoint_executor


def build_governance_workflow(
    session_state: dict[str, Any] | None = None,
    db: Any | None = None,
    routing_enabled: bool = False,
) -> Workflow:
    """Build the Agno Workflow that defines the Qwendom governance lifecycle.

    The orchestrator owns event emission and deterministic local behavior, but
    the ordered Workflow object is the canonical V3 lifecycle spine. Each
    checkpoint mutates shared session_state so failed runs can be inspected and
    resumed by phase.
    """

    if session_state is not None:
        prompt = str(session_state.get("task_prompt", ""))
        routing = build_routing_metadata(prompt, routing_enabled)
        session_state["task_class"] = routing["task_class"]
        session_state["workflow_routing"] = routing

    return Workflow(
        name="Qwendom Governance Workflow",
        description="Ordered V3 governance phases for a task society run.",
        db=db,
        session_state=session_state,
        steps=[
            Step(name=phase, executor=_checkpoint_executor_for(phase), max_retries=0, on_error="raise")
            for phase in GOVERNANCE_PHASES
        ],
        add_session_state_to_context=True,
    )


def compute_task_metrics(
    session_state: dict[str, Any],
    start_time: float,
) -> TaskMetrics:
    """Compute structured evaluation metrics from a completed session_state.

    Called at the end of a governance run to produce a TaskMetrics snapshot
    that is both emitted as a JSONL event and stored for the /metrics endpoint.
    """

    ballots = session_state.get("ballots", [])
    tally = session_state.get("tally", {})
    winner_votes = max(tally.values()) if tally else 0
    total_votes = sum(tally.values()) if tally else 1

    metrics = session_state.get("metrics", {})
    critique = session_state.get("critique") or {}

    return TaskMetrics(
        task_id=session_state.get("task_id", ""),
        total_duration_seconds=round(time.time() - start_time, 3),
        governance_rounds=metrics.get("governance_rounds", 0),
        debate_rounds=metrics.get("debate_rounds", 0),
        tool_calls_total=metrics.get("tool_calls", 0),
        tool_calls_failed=metrics.get("tool_calls_failed", 0),
        proposals_count=len(session_state.get("proposals", {})),
        challenges_count=len(session_state.get("challenges", [])),
        revisions_count=len(session_state.get("revisions", {})),
        vote_margin=winner_votes / total_votes if total_votes else 0.0,
        critique_risk_count=len(critique.get("risks", []) if isinstance(critique, dict) else []),
        child_agents_spawned=len(session_state.get("child_agents", [])),
        memory_writes=metrics.get("memory_writes", 0),
        answer_length=len(session_state.get("final_answer", "")),
        evaluation_metrics=list(session_state.get("evaluation_metrics", [])),
    )
