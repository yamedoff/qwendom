from __future__ import annotations

import json
from uuid import uuid4

from agno.run import RunContext
from agno.tools import tool


@tool(name="assign_subtask", stop_after_tool_call=True)
def assign_subtask_tool(
    run_context: RunContext,
    agent_id: str,
    subtask: str,
    subtask_id: str = "",
    deadline_step: int = 0,
    why_assigned: str = "",
    done_criteria: list[str] | None = None,
    blocking_if_missing: bool = False,
    provenance: str = "",
) -> str:
    """Assign a subtask to an agent in the shared session state.

    Args:
        agent_id: The id of the agent receiving the subtask.
        subtask: Description of the work to perform.
        subtask_id: Optional stable id of an existing planned subtask to transition.
        deadline_step: Workflow step index by which the subtask should complete.
        why_assigned: Rationale for routing this work to this agent.
        done_criteria: Concrete conditions that define completion.
        blocking_if_missing: Whether this subtask blocks the overall run if not completed.
        provenance: Origin trace: decomposition, working_brief, coordination_brief, or leader_plan.

    Returns:
        JSON confirmation the subtask was assigned.
    """
    subtasks = run_context.session_state.setdefault("subtasks", [])
    existing = next(
        (
            item
            for item in subtasks
            if (
                subtask_id
                and item.get("id") == subtask_id
                and item.get("agent_id") == agent_id
            )
            or (
                not subtask_id
                and
                item.get("agent_id") == agent_id
                and item.get("status") == "planned"
                and item.get("subtask") == subtask
            )
        ),
        None,
    )
    record = existing or {
        "id": subtask_id or f"subtask-{uuid4().hex[:10]}",
        "agent_id": agent_id,
        "subtask": subtask,
    }
    record.setdefault("id", subtask_id or f"subtask-{uuid4().hex[:10]}")
    record["agent_id"] = agent_id
    record["subtask"] = subtask
    record["deadline_step"] = deadline_step
    record["status"] = "assigned"
    if why_assigned:
        record["why_assigned"] = why_assigned
    if done_criteria:
        record["done_criteria"] = list(done_criteria)
    record["blocking_if_missing"] = bool(blocking_if_missing)
    if provenance:
        record["provenance"] = provenance
    if existing is None:
        subtasks.append(record)
    return json.dumps({
        "id": record["id"],
        "subtask_id": record["id"],
        "status": "assigned",
        "agent_id": agent_id,
        "subtask": subtask,
        "deadline_step": deadline_step,
        "why_assigned": why_assigned,
        "done_criteria": record.get("done_criteria", []),
        "blocking_if_missing": record.get("blocking_if_missing", False),
        "provenance": record.get("provenance", provenance),
    })


@tool(name="report_subtask", stop_after_tool_call=True)
def report_subtask_tool(
    run_context: RunContext,
    subtask_id: str,
    agent_id: str,
    result: str,
    blockers: list[str] | None = None,
    result_summary: str = "",
    evidence_refs: list[str] | None = None,
    outcome_status: str = "",
    outcome_summary: str = "",
    provenance: str = "",
) -> str:
    """Report completion of an assigned subtask.

    Args:
        subtask_id: Stable id of the assigned subtask.
        agent_id: The id of the reporting agent.
        result: Summary of the completed work.
        blockers: Any blockers encountered during execution.
        result_summary: Compact one-line summary for projections.
        evidence_refs: References to evidence used (memory ids, context7 refs, artifact ids).
        outcome_status: Structured outcome label: completed, partial, blocked, or failed.
        outcome_summary: One-line outcome for cockpit and recap views.
        provenance: Origin trace for the reported work.

    Returns:
        JSON confirmation the subtask report was recorded.
    """
    subtasks = run_context.session_state.get("subtasks", [])
    status = "blocked" if blockers else "completed"
    for st in subtasks:
        if st.get("id") == subtask_id and st.get("agent_id") == agent_id and st.get("status") == "assigned":
            st["status"] = status
            st["result"] = result
            st["blockers"] = blockers or []
            if result_summary:
                st["result_summary"] = result_summary
            if evidence_refs:
                st["evidence_refs"] = list(evidence_refs)
            if outcome_status:
                st["outcome_status"] = outcome_status
            if outcome_summary:
                st["outcome_summary"] = outcome_summary
            if provenance:
                st["provenance"] = provenance
            return json.dumps({
                "id": subtask_id,
                "status": status,
                "agent_id": agent_id,
                "result": result,
                "blockers": blockers or [],
                "result_summary": st.get("result_summary", result_summary),
                "evidence_refs": st.get("evidence_refs", evidence_refs or []),
                "outcome_status": st.get("outcome_status", outcome_status or status),
                "outcome_summary": st.get("outcome_summary", outcome_summary),
                "provenance": st.get("provenance", provenance),
            })
    return json.dumps({
        "id": subtask_id,
        "status": "not_found",
        "agent_id": agent_id,
        "result": result,
        "blockers": blockers or [],
        "result_summary": result_summary,
        "evidence_refs": evidence_refs or [],
        "outcome_status": outcome_status or "not_found",
        "outcome_summary": outcome_summary,
        "provenance": provenance,
    })
