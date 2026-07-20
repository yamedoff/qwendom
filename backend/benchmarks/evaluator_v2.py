"""Deterministic evaluator for the frozen multi-ask v2 suite."""

from __future__ import annotations

from typing import Any

from benchmarks.fixtures.answers_v2 import ANSWER_KEYS
from benchmarks.suite_v2 import BenchmarkAnswer, TASKS


def _f1(actual: set[str], expected: set[str]) -> float:
    """Return exact-ID F1, treating two empty sets as a perfect match."""
    if not actual and not expected:
        return 1.0
    if not actual or not expected:
        return 0.0
    overlap = len(actual & expected)
    precision, recall = overlap / len(actual), overlap / len(expected)
    return 2 * precision * recall / (precision + recall) if overlap else 0.0


def evaluate_task(task_id: str, answer: BenchmarkAnswer) -> dict[str, Any]:
    """Score one answer out of 100 without mode-dependent logic."""
    task, key = TASKS[task_id], ANSWER_KEYS[task_id]
    selection = 30.0 * _f1(set(answer.selected_ids), key.selected_ids)
    order = 20.0 if not key.ordered_ids else 20.0 * _ordered_prefix(answer.ordered_ids, key.ordered_ids)
    claim_scores = [
        _f1(set(answer.evidence_citations.get(label, [])), key.evidence_citations[label])
        for label in task.required_claim_labels
    ]
    evidence = 30.0 * (sum(claim_scores) / len(claim_scores))
    constraint_parts: list[float] = []
    if key.constraint_ids:
        constraint_parts.append(_f1(set(answer.constraint_ids), key.constraint_ids))
    for label, expected in key.numeric_answers.items():
        actual = answer.numeric_answers.get(label)
        constraint_parts.append(float(actual is not None and abs(actual - expected) <= key.numeric_tolerance))
    constraints = 20.0 * (sum(constraint_parts) / len(constraint_parts)) if constraint_parts else 20.0
    total = round(selection + order + evidence + constraints, 4)
    return {
        "suite_version": "v2", "task_id": task_id, "kind": task.kind,
        "score": total,
        "categories": {
            "selection": round(selection, 4), "order": round(order, 4),
            "evidence": round(evidence, 4), "constraints": round(constraints, 4),
        },
    }


def _ordered_prefix(actual: list[str], expected: list[str]) -> float:
    """Award proportional credit until the first incorrect ordering choice."""
    correct = 0
    for got, wanted in zip(actual, expected):
        if got != wanted:
            break
        correct += 1
    return correct / len(expected)


def macro_average(results: list[dict[str, Any]]) -> float:
    """Return an equal-weight task average; reject incomplete suites."""
    expected = set(TASKS)
    actual = {str(result["task_id"]) for result in results}
    if actual != expected or len(results) != len(expected):
        raise ValueError(f"complete suite required: expected={sorted(expected)} got={sorted(actual)}")
    return round(sum(float(result["score"]) for result in results) / len(results), 4)
