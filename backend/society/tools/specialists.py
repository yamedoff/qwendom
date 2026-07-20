"""Safe leader tools for selecting predefined fixed specialist templates."""

from __future__ import annotations

import json
from typing import Any

from agno.run import RunContext
from agno.tools import tool

from ..specialist_selection import (
    InvokeSpecialistCall,
    ListSpecialistsResult,
    SelectSpecialistsCall,
    SpecialistAssignmentSelection,
)


@tool(name="list_specialists")
def list_specialists_tool(run_context: RunContext | None = None) -> str:
    """Return the current secret-safe fixed specialist catalog.

    The runtime supplies this catalog in session state after verifying current
    tool and provider availability. The leader cannot alter catalog entries.
    """

    catalog = [] if run_context is None else run_context.session_state.get("fixed_specialist_catalog", [])
    return ListSpecialistsResult.model_validate({"specialists": catalog}).model_dump_json()


@tool(name="select_specialists", stop_after_tool_call=True)
def select_specialists_tool(
    assignments: list[dict[str, Any]],
    selection_rationale: str,
    run_context: RunContext | None = None,
) -> str:
    """Record the leader's bounded team selection.

    Args:
        assignments: Specialist template IDs, objectives, dependencies, owned
            artifacts, and acceptance requirements. Tool IDs, skill IDs,
            credentials, locks, and sandbox policy are not accepted fields.
        selection_rationale: Why this is the smallest capable fixed team.
    """

    call = SelectSpecialistsCall(
        assignments=[SpecialistAssignmentSelection.model_validate(item) for item in assignments],
        selection_rationale=selection_rationale,
    )
    if run_context is not None:
        run_context.session_state["fixed_specialist_selection_call"] = call.model_dump(mode="json")
    return call.model_dump_json()


@tool(name="invoke_specialist", stop_after_tool_call=True)
def invoke_specialist_tool(
    assignment_id: str,
    run_context: RunContext | None = None,
) -> str:
    """Record invocation of one assignment from the accepted selection."""

    call = InvokeSpecialistCall(assignment_id=assignment_id)
    if run_context is not None:
        selected = run_context.session_state.get("fixed_specialist_selection_call", {})
        selected_ids = {
            str(item.get("assignment_id"))
            for item in selected.get("assignments", [])
            if isinstance(item, dict)
        } if isinstance(selected, dict) else set()
        if assignment_id not in selected_ids:
            raise ValueError(f"unknown_specialist_assignment:{assignment_id}")
        selected_assignment = next(
            item for item in selected.get("assignments", [])
            if isinstance(item, dict) and str(item.get("assignment_id")) == assignment_id
        )
        completed = {
            str(item) for item in run_context.session_state.get("fixed_specialist_completed_assignments", [])
        }
        unmet = sorted({str(item) for item in selected_assignment.get("depends_on", [])} - completed)
        if unmet:
            raise ValueError(f"specialist_dependencies_unmet:{assignment_id}:{unmet}")
        invoked = run_context.session_state.setdefault("fixed_specialist_invocations", [])
        if assignment_id not in invoked:
            invoked.append(assignment_id)
    return call.model_dump_json()
