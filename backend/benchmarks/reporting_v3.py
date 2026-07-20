"""Quality and efficiency reporting for benchmark suite v3."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from benchmarks.runtime_v3 import SuiteV3TrialResult


def aggregate_mode(trials: list[SuiteV3TrialResult]) -> dict[str, Any]:
    """Aggregate quality, cost, latency, and throughput without hiding failures."""

    successes = [trial for trial in trials if trial.status == "success" and trial.evaluation]
    scores = [float(trial.evaluation["score"]) for trial in successes]
    passed_checks = sum(
        int(trial.evaluation["acceptance_checks_passed"]) for trial in successes
    )
    duration = sum(trial.wall_duration_s for trial in trials)
    usage_complete = bool(trials) and all(trial.usage.usage_complete for trial in trials)
    total_tokens = sum(trial.usage.total_tokens or 0 for trial in trials) if usage_complete else None
    return {
        "attempts": len(trials),
        "successes": len(successes),
        "failures": len(trials) - len(successes),
        "mean_score": statistics.mean(scores) if scores else None,
        "score_range": [min(scores), max(scores)] if scores else None,
        "acceptance_checks_passed": passed_checks,
        "acceptance_checks_available": 24 * len(successes),
        "total_duration_seconds": duration,
        "mean_duration_seconds": statistics.mean(
            [trial.wall_duration_s for trial in trials]
        ) if trials else None,
        "model_calls": sum(trial.usage.model_calls or 0 for trial in trials),
        "total_tokens": total_tokens,
        "tool_calls": sum(len(trial.tool_calls) for trial in trials),
        "checks_per_minute": (passed_checks / (duration / 60.0)) if duration > 0 else None,
        "checks_per_million_tokens": (
            passed_checks / (total_tokens / 1_000_000.0)
            if total_tokens and total_tokens > 0 else None
        ),
        "usage_complete": usage_complete,
    }


def compare_modes(
    single: list[SuiteV3TrialResult],
    society: list[SuiteV3TrialResult],
) -> dict[str, Any]:
    """Return society-minus-single deltas for quality and both efficiency measures."""

    left, right = aggregate_mode(single), aggregate_mode(society)

    def delta(field: str) -> float | None:
        if left[field] is None or right[field] is None:
            return None
        return right[field] - left[field]

    return {
        "suite_version": "v3",
        "single_agent": left,
        "society": right,
        "quality_delta": delta("mean_score"),
        "checks_per_minute_delta": delta("checks_per_minute"),
        "checks_per_million_tokens_delta": delta("checks_per_million_tokens"),
    }


def write_bundle(
    output: Path,
    single: list[SuiteV3TrialResult],
    society: list[SuiteV3TrialResult],
) -> Path:
    """Write every attempt and one reproducible machine-readable comparison."""

    output.mkdir(parents=True, exist_ok=True)
    for mode, trials in (("single_agent", single), ("society", society)):
        folder = output / mode
        folder.mkdir(exist_ok=True)
        for index, trial in enumerate(trials, 1):
            (folder / f"trial-{index:03d}.json").write_text(
                trial.model_dump_json(indent=2), encoding="utf-8"
            )
    (output / "comparison.json").write_text(
        json.dumps(compare_modes(single, society), indent=2), encoding="utf-8"
    )
    return output
