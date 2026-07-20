"""Benchmark slice 1 — deterministic single-agent vs society reference-task contract.

This package provides a no-sandbox, no-LLM evaluation harness for comparing
single-agent and society-mode incident-response quality on an identical
reference task.  The public API surface is:

* :class:`IncidentDecision` — strict JSON schema that both modes must emit.
* :class:`EvaluationResult` — detailed per-check scoring artefact.
* :func:`evaluate` — pure 100-point rubric, mode-neutral.
* :func:`build_prompt` — constructs the model-facing prompt from the public
  incident packet only (ground truth is never included).
* :func:`load_incident_packet` / :func:`load_ground_truth` — fixture loaders.

Fairness guarantees
-------------------
* The same 100-point rubric is applied regardless of ``mode`` label.
* Governance activity is explicitly excluded from scoring.
* All comparisons are exact ID / set / order — never prose-keyword matching.
* Evidence citations are validated against the public fact-ID universe.
* Unsupported or invalid fact IDs are penalised.
* Scores are clamped to [0, 100].
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Mode = Literal["single_agent", "society"]


class IncidentDecision(BaseModel):
    """Structured decision emitted by either evaluation mode.

    Every field uses opaque string IDs that are compared by exact match,
    set equality, or ordered sequence — never by substring or keyword.
    """

    root_cause_hypothesis_id: str = Field(
        description="Exact ID of the selected root-cause hypothesis.",
    )
    hypothesis_evidence: list[str] = Field(
        default_factory=list,
        description="Fact IDs cited in support of the root-cause hypothesis.",
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Ordered list of action IDs to remediate the incident.",
    )
    recommended_controls: list[str] = Field(
        default_factory=list,
        description="Set of control IDs to prevent recurrence.",
    )
    evidence_citations: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Mapping of claim label to supporting fact IDs.",
    )
    stakeholder_constraints_acknowledged: list[str] = Field(
        default_factory=list,
        description="Constraint IDs the decision claims to satisfy.",
    )
    governance_activity: list[str] = Field(
        default_factory=list,
        description="Governance steps taken. Explicitly ignored by the scorer.",
    )


class CheckResult(BaseModel):
    """Single rubric check with pass/fail and detail."""

    check_id: str
    category: str
    passed: bool
    points_awarded: float
    points_possible: float
    detail: str = ""


class EvaluationResult(BaseModel):
    """Detailed scoring artefact produced by the pure evaluator."""

    mode: Mode
    total_score: float = Field(ge=0.0, le=100.0)
    root_cause_score: float = 0.0
    actions_score: float = 0.0
    controls_score: float = 0.0
    evidence_score: float = 0.0
    constraints_score: float = 0.0
    governance_score: float = Field(default=0.0, description="Always 0 — governance must not affect score.")
    per_check_results: list[CheckResult] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    penalty_total: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


def load_ground_truth() -> dict[str, Any]:
    """Load the private ground-truth fixture."""
    from benchmarks.fixtures.ground_truth import get_ground_truth
    return get_ground_truth()


def evaluate(decision: IncidentDecision, mode: Mode = "single_agent") -> EvaluationResult:
    """Score an IncidentDecision against the ground truth (delegated to evaluator)."""
    from benchmarks.evaluator import evaluate as _evaluate
    return _evaluate(decision, mode)
