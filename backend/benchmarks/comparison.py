"""Pure mode-neutral comparison and efficiency helpers.

All functions in this module are deterministic, side-effect-free, and
treat both ``"single_agent"`` and ``"society"`` modes identically.
"""

from __future__ import annotations

from typing import Any

from benchmarks import EvaluationResult


def score_delta(a: EvaluationResult, b: EvaluationResult) -> float:
    """Return ``a.total_score - b.total_score``."""
    return round(a.total_score - b.total_score, 4)


def winner(a: EvaluationResult, b: EvaluationResult) -> str | None:
    """Return the mode label of the higher-scoring result, or ``None`` on tie."""
    if a.total_score > b.total_score:
        return a.mode
    if b.total_score > a.total_score:
        return b.mode
    return None


def is_tie(a: EvaluationResult, b: EvaluationResult, tolerance: float = 0.001) -> bool:
    """Return ``True`` if the two scores are within *tolerance*."""
    return abs(a.total_score - b.total_score) <= tolerance


def per_category_breakdown(result: EvaluationResult) -> dict[str, float]:
    """Return a mapping of category name to points awarded."""
    return {
        "root_cause": result.root_cause_score,
        "actions": result.actions_score,
        "controls": result.controls_score,
        "evidence": result.evidence_score,
        "constraints": result.constraints_score,
        "governance": result.governance_score,
    }


def check_pass_rate(result: EvaluationResult) -> float:
    """Return the fraction of checks that passed."""
    if not result.per_check_results:
        return 0.0
    passed = sum(1 for c in result.per_check_results if c.passed)
    return round(passed / len(result.per_check_results), 4)


def compare_results(
    a: EvaluationResult, b: EvaluationResult
) -> dict[str, Any]:
    """Return a structured comparison of two evaluation results."""
    return {
        "score_a": a.total_score,
        "score_b": b.total_score,
        "delta": score_delta(a, b),
        "winner": winner(a, b),
        "tie": is_tie(a, b),
        "pass_rate_a": check_pass_rate(a),
        "pass_rate_b": check_pass_rate(b),
        "breakdown_a": per_category_breakdown(a),
        "breakdown_b": per_category_breakdown(b),
    }
