"""Twenty-four deterministic acceptance checks for benchmark v3."""

from __future__ import annotations

from typing import Any

from benchmarks.fixtures.answers_v3 import (
    EXPECTED_CONSTRAINTS, EXPECTED_EVIDENCE, EXPECTED_FINDINGS,
    EXPECTED_NUMERICS, EXPECTED_STAGES,
)
from benchmarks.suite_v2 import BenchmarkAnswer


def evaluate(answer: BenchmarkAnswer) -> dict[str, Any]:
    """Score seven findings, four stages, seven citations, three constraints, and three numbers."""

    checks: dict[str, bool] = {}
    selected = set(answer.selected_ids)
    for finding_id in sorted(EXPECTED_FINDINGS):
        checks[f"finding:{finding_id}"] = finding_id in selected and not bool(selected - EXPECTED_FINDINGS)
    for index, stage_id in enumerate(EXPECTED_STAGES):
        checks[f"stage:{stage_id}"] = len(answer.ordered_ids) > index and answer.ordered_ids[index] == stage_id
    for label, record_ids in EXPECTED_EVIDENCE.items():
        checks[f"evidence:{label}"] = set(answer.evidence_citations.get(label, [])) == record_ids
    constraints = set(answer.constraint_ids)
    for constraint_id in sorted(EXPECTED_CONSTRAINTS):
        checks[f"constraint:{constraint_id}"] = constraint_id in constraints and not bool(constraints - EXPECTED_CONSTRAINTS)
    for label, expected in EXPECTED_NUMERICS.items():
        checks[f"numeric:{label}"] = abs(float(answer.numeric_answers.get(label, float("inf"))) - expected) <= 0.01
    passed = sum(checks.values())
    return {
        "suite_version": "v3", "task_id": "security-release-audit",
        "score": round(100.0 * passed / 24.0, 4),
        "acceptance_checks_passed": passed, "acceptance_checks_total": 24,
        "checks": checks,
    }
