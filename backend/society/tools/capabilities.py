from __future__ import annotations

from typing import Any
from uuid import uuid4

from agno.run import RunContext
from agno.tools import tool

from ..schemas.capabilities import (
    ImplementationPlan,
    MemoryLookup,
    MemoryWrite,
    RiskAssessment,
    TaskDecomposition,
)


def _as_list(value: list[Any] | str | None) -> list[str]:
    """Normalize model-provided list arguments that arrive as bullet strings."""

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]


@tool(name="decompose_task", stop_after_tool_call=True)
def decompose_task_tool(
    objective: str,
    steps: list[str] | str,
    delegation_plan: dict[str, str] | None = None,
    run_context: RunContext | None = None,
) -> str:
    """Record a task decomposition for the current team.

    Args:
        objective: The clarified objective the team should solve.
        steps: Ordered work steps.
        delegation_plan: Mapping from agent id or role to responsibility.

    Returns:
        JSON string with the validated decomposition.
    """

    result = TaskDecomposition(
        objective=objective,
        steps=_as_list(steps),
        delegation_plan=delegation_plan or {},
    )
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("subtasks", []).extend(
            {
                "id": f"subtask-{uuid4().hex[:10]}",
                "agent_id": agent_id,
                "subtask": subtask,
                "status": "planned",
            }
            for agent_id, subtask in result.delegation_plan.items()
        )
    return result.model_dump_json()


@tool(name="memory_lookup", stop_after_tool_call=True)
def memory_lookup_tool(
    query: str,
    relevant_memories: list[Any] | str | None,
    lesson: str,
    run_context: RunContext | None = None,
) -> str:
    """Record context retrieved from an agent's collaboration memory.

    Args:
        query: The memory/context question being answered.
        relevant_memories: Relevant remembered facts or prior lessons.
        lesson: The main lesson to apply to the current task.

    Returns:
        JSON string with the validated memory lookup.
    """

    result = MemoryLookup(query=query, relevant_memories=_as_list(relevant_memories), lesson=lesson)
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("memory_reads", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="implementation_plan", stop_after_tool_call=True)
def implementation_plan_tool(
    artifact: str,
    milestones: list[str] | str,
    acceptance_checks: list[str] | str,
    run_context: RunContext | None = None,
) -> str:
    """Record a concrete implementation plan.

    Args:
        artifact: The artifact or deliverable to produce.
        milestones: Ordered implementation milestones.
        acceptance_checks: Checks proving the artifact is useful.

    Returns:
        JSON string with the validated implementation plan.
    """

    result = ImplementationPlan(
        artifact=artifact,
        milestones=_as_list(milestones),
        acceptance_checks=_as_list(acceptance_checks),
    )
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("implementation_plans", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="risk_assessment", stop_after_tool_call=True)
def risk_assessment_tool(
    risks: list[str] | str,
    mitigations: list[str] | str,
    quality_gate: str,
    run_context: RunContext | None = None,
) -> str:
    """Record risks and quality gates for the proposed solution.

    Args:
        risks: Important failure modes or unsupported assumptions.
        mitigations: Practical mitigations for the listed risks.
        quality_gate: The minimum check that must pass before accepting the work.

    Returns:
        JSON string with the validated risk assessment.
    """

    result = RiskAssessment(risks=_as_list(risks), mitigations=_as_list(mitigations), quality_gate=quality_gate)
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("risk_assessments", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="memory_write", stop_after_tool_call=True)
def memory_write_tool(memory: str, tags: list[str] | str | None = None, run_context: RunContext | None = None) -> str:
    """Record a durable collaboration lesson.

    Args:
        memory: The lesson or collaboration fact to preserve.
        tags: Optional labels for future retrieval.

    Returns:
        JSON string with the validated memory write.
    """

    result = MemoryWrite(memory=memory, tags=_as_list(tags))
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("memory_writes", []).append(result.model_dump())
    return result.model_dump_json()
