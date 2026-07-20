"""Ground-truth fixture — NEVER included in the model prompt.

This module defines the correct answers for the reference incident.  It is
imported only by the evaluator and the test suite, never by the loader or
prompt builder.
"""

from __future__ import annotations

from typing import Any

GROUND_TRUTH_HYPOTHESIS_ID: str = "H02"

GROUND_TRUTH_HYPOTHESIS_EVIDENCE: list[str] = [
    "F04",
    "F05",
    "F06",
    "F07",
    "F17",
    "F19",
    "F21",
]

GROUND_TRUTH_ACTIONS: list[str] = ["A01", "A03"]

GROUND_TRUTH_ACTION_ORDER: list[str] = ["A01", "A03"]

GROUND_TRUTH_CONTROLS: list[str] = ["C02", "C04"]

GROUND_TRUTH_REQUIRED_CLAIMS: dict[str, list[str]] = {
    "deployment_cause": ["F04", "F05"],
    "connection_exhaustion": ["F17", "F19"],
    "cascading_failure": ["F08", "F21"],
}

GROUND_TRUTH_CONSTRAINT_SATISFACTION: dict[str, bool] = {
    "SC01": True,
    "SC02": True,
    "SC03": True,
}

GROUND_TRUTH_INVALID_FACT_IDS: list[str] = []

VALID_FACT_IDS: set[str] = {
    f"F{i:02d}" for i in range(1, 26)
}


def get_ground_truth() -> dict[str, Any]:
    """Return the full ground-truth fixture as a dict."""
    return {
        "hypothesis_id": GROUND_TRUTH_HYPOTHESIS_ID,
        "hypothesis_evidence": GROUND_TRUTH_HYPOTHESIS_EVIDENCE,
        "actions": GROUND_TRUTH_ACTIONS,
        "action_order": GROUND_TRUTH_ACTION_ORDER,
        "controls": GROUND_TRUTH_CONTROLS,
        "required_claims": GROUND_TRUTH_REQUIRED_CLAIMS,
        "constraint_satisfaction": GROUND_TRUTH_CONSTRAINT_SATISFACTION,
        "valid_fact_ids": sorted(VALID_FACT_IDS),
    }
