from __future__ import annotations

import json
from datetime import datetime, timezone

from agno.run import RunContext
from agno.tools import tool

from ..schemas.evaluation import IndependentValidationReport


@tool(name="record_metric", stop_after_tool_call=True)
def record_metric_tool(
    run_context: RunContext,
    metric_name: str,
    value: float,
    context: str = "",
) -> str:
    """Record an evaluation metric into the shared session state.

    Args:
        metric_name: Name of the metric being recorded.
        value: Numeric value of the metric.
        context: Optional context about why this metric was recorded.

    Returns:
        JSON confirmation the metric was recorded.
    """
    metric = {
        "metric_name": metric_name,
        "value": value,
        "context": context,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    run_context.session_state.setdefault("evaluation_metrics", []).append(metric)
    return json.dumps({"status": "recorded", "metric": metric_name, **metric})


@tool(name="report_independent_validation", stop_after_tool_call=True)
def report_independent_validation_tool(
    passed: bool,
    checked_evidence_ids: list[str],
    missing_evidence: list[str],
    contradictions: list[str],
    recommendation: str,
    run_context: RunContext | None = None,
) -> str:
    """Record a non-voting specialist's independent proof review."""
    report = IndependentValidationReport(
        passed=passed,
        checked_evidence_ids=checked_evidence_ids,
        missing_evidence=missing_evidence,
        contradictions=contradictions,
        recommendation=recommendation,
    )
    if run_context is not None:
        run_context.session_state["independent_validation"] = report.model_dump()
    return report.model_dump_json()
