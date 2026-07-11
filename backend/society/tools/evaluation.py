from __future__ import annotations

import json
from datetime import datetime, timezone

from agno.run import RunContext
from agno.tools import tool


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
