"""Versioned, multi-ask benchmark contracts and frozen public fixtures.

The public task contains everything a model may see. Expected answers live in
``ANSWER_KEYS`` and must only be imported by deterministic evaluation code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


TaskKind = Literal[
    "incident_diagnosis",
    "plan_prioritization",
    "evidence_verification",
    "constraint_aware_decision",
]


class BenchmarkAnswer(BaseModel):
    """One mode-neutral answer shape shared by every v2 task."""

    selected_ids: list[str] = Field(default_factory=list)
    ordered_ids: list[str] = Field(default_factory=list)
    evidence_citations: dict[str, list[str]] = Field(default_factory=dict)
    constraint_ids: list[str] = Field(default_factory=list)
    numeric_answers: dict[str, float] = Field(default_factory=dict)
    governance_activity: list[str] = Field(default_factory=list)


class BenchmarkTask(BaseModel):
    """Frozen public input for one benchmark ask."""

    suite_version: str = "v2"
    task_id: str
    kind: TaskKind
    ask: str
    records: dict[str, str]
    candidates: dict[str, str]
    constraints: dict[str, str] = Field(default_factory=dict)
    required_claim_labels: list[str]
    required_numeric_labels: list[str] = Field(default_factory=list)


class TaskAnswerKey(BaseModel):
    """Private exact-match key used only by the deterministic evaluator."""

    selected_ids: set[str]
    ordered_ids: list[str] = Field(default_factory=list)
    evidence_citations: dict[str, set[str]]
    constraint_ids: set[str] = Field(default_factory=set)
    numeric_answers: dict[str, float] = Field(default_factory=dict)
    numeric_tolerance: float = 0.01


TASKS: dict[str, BenchmarkTask] = {
    "incident-diagnosis": BenchmarkTask(
        task_id="incident-diagnosis",
        kind="incident_diagnosis",
        ask="Identify the root cause, order the immediate actions, and prove each required claim.",
        records={
            "F01": "Release v2.4.1 completed at 02:05 UTC.",
            "F02": "The release changed database connections per pod from 10 to 50.",
            "F03": "Eight pods were active before scaling.",
            "F04": "Database connections reached 475 of 500 at 02:10 UTC.",
            "F05": "Service latency rose from 50 ms to 28,000 ms.",
            "F06": "Network latency stayed below 5 ms.",
            "F07": "The previous release had stable connection counts.",
        },
        candidates={
            "H01": "Network partition.",
            "H02": "New per-pod connection allocation exhausted the pool.",
            "H03": "Organic traffic exhausted the pool.",
            "A01": "Rollback to v2.4.0.",
            "A02": "Restore the 10-connection setting and redeploy.",
            "A03": "Renew the TLS certificate.",
        },
        constraints={"C01": "No data loss.", "C02": "No schema change."},
        required_claim_labels=["deployment_cause", "connection_exhaustion", "service_impact"],
    ),
    "plan-prioritization": BenchmarkTask(
        task_id="plan-prioritization",
        kind="plan_prioritization",
        ask="Select and order the work that reaches a safe demo fastest, then cite why the order is necessary.",
        records={
            "P01": "The API crashes on a missing provider key.",
            "P02": "The demo UI depends on the API health endpoint.",
            "P03": "A scripted demo takes 20 minutes to record after the API works.",
            "P04": "A color-polish task takes 90 minutes and changes no behavior.",
            "P05": "Provider preflight can report the missing key in 15 minutes.",
            "P06": "The submission requires a truthful failure state.",
            "P07": "The API-to-UI first-run verification takes 10 minutes.",
        },
        candidates={
            "W01": "Add provider preflight and typed failure.",
            "W02": "Verify API-to-UI first-run flow.",
            "W03": "Record the demo.",
            "W04": "Polish colors.",
        },
        constraints={"C01": "No fabricated success.", "C02": "Finish within 60 minutes."},
        required_claim_labels=["blocker_first", "dependency_order", "scope_control"],
        required_numeric_labels=["minimum_minutes"],
    ),
    "evidence-verification": BenchmarkTask(
        task_id="evidence-verification",
        kind="evidence_verification",
        ask=(
            "Put only positively proven candidate claim IDs in selected_ids and exclude unsupported claims. "
            "Leave ordered_ids empty because this task has no ordering requirement. Cite positive evidence for "
            "the proven claims, and use deployment_gap to cite records showing why CL03 must be excluded."
        ),
        records={
            "E01": "Three backend tests passed.",
            "E02": "The browser rendered 175 persisted events after restart.",
            "E03": "The deployment guide exists but contains no public URL.",
            "E04": "A real Qwen response includes model qwen3.7-plus and token usage.",
            "E05": "No signed-out public-link check was performed.",
        },
        candidates={
            "CL01": "Local restart recovery is proven.",
            "CL02": "Direct Qwen use is proven.",
            "CL03": "Public deployment is proven.",
            "CL04": "Automated tests are proven.",
        },
        required_claim_labels=["restart_proof", "provider_proof", "test_proof", "deployment_gap"],
    ),
    "constraint-aware-decision": BenchmarkTask(
        task_id="constraint-aware-decision",
        kind="constraint_aware_decision",
        ask="Choose the eligible option with the highest quality score and prove eligibility and selection.",
        records={
            "D01": "Option O1 scores 91, costs $120, and takes 3 days.",
            "D02": "Option O2 scores 88, costs $80, and takes 2 days.",
            "D03": "Option O3 scores 84, costs $45, and takes 1 day.",
            "D04": "The budget limit is $90.",
            "D05": "The deadline is 2 days.",
        },
        candidates={"O1": "Highest quality.", "O2": "Balanced.", "O3": "Lowest cost."},
        constraints={"C01": "Cost at most $90.", "C02": "Time at most 2 days."},
        required_claim_labels=["budget_eligibility", "deadline_eligibility", "quality_selection"],
        required_numeric_labels=["selected_quality", "selected_cost", "selected_days"],
    ),
}


def get_task(task_id: str) -> BenchmarkTask:
    """Return a frozen public task or fail clearly for an unknown ID."""
    return TASKS[task_id].model_copy(deep=True)
