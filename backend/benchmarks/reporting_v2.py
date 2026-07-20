"""Equal-weight reporting for multi-ask benchmark suite v2."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from benchmarks.runtime_v2 import SuiteTrialResult
from benchmarks.suite_v2 import TASKS


def aggregate_mode(trials: list[SuiteTrialResult]) -> dict[str, Any]:
    """Aggregate by task first, then macro-average task means equally."""
    per_task: dict[str, Any] = {}
    for task_id in TASKS:
        attempts = [trial for trial in trials if trial.task_id == task_id]
        successes = [trial for trial in attempts if trial.status == "success" and trial.evaluation]
        scores = [float(trial.evaluation["score"]) for trial in successes]
        per_task[task_id] = {
            "attempts": len(attempts), "successes": len(successes),
            "failures": len(attempts) - len(successes),
            "mean_score": statistics.mean(scores) if scores else None,
        }
    task_means = [entry["mean_score"] for entry in per_task.values()]
    complete = all(value is not None for value in task_means)
    successes = [trial for trial in trials if trial.status == "success"]
    usage_complete = bool(trials) and all(trial.usage.usage_complete for trial in trials)
    return {
        "attempts": len(trials), "successes": len(successes),
        "failures": len(trials) - len(successes), "per_task": per_task,
        "macro_mean_score": statistics.mean(task_means) if complete else None,
        "mean_duration_seconds": statistics.mean([t.wall_duration_s for t in trials]) if trials else None,
        "model_calls": sum(t.usage.model_calls or 0 for t in trials),
        "total_tokens": sum(t.usage.total_tokens or 0 for t in trials) if usage_complete else None,
        "tool_calls": sum(len(t.tool_calls) for t in successes),
    }


def compare_modes(single: list[SuiteTrialResult], society: list[SuiteTrialResult]) -> dict[str, Any]:
    """Compare suite macro scores without hiding per-task results."""
    left, right = aggregate_mode(single), aggregate_mode(society)
    delta = None
    if left["macro_mean_score"] is not None and right["macro_mean_score"] is not None:
        delta = right["macro_mean_score"] - left["macro_mean_score"]
    return {"suite_version": "v2", "single_agent": left, "society": right, "quality_delta": delta}


def write_bundle(output: Path, single: list[SuiteTrialResult], society: list[SuiteTrialResult]) -> Path:
    """Write every attempt plus a machine-readable comparison."""
    output.mkdir(parents=True, exist_ok=True)
    for mode, trials in (("single_agent", single), ("society", society)):
        folder = output / mode; folder.mkdir(exist_ok=True)
        for index, trial in enumerate(trials, 1):
            (folder / f"trial-{index:03d}.json").write_text(trial.model_dump_json(indent=2), encoding="utf-8")
    comparison = compare_modes(single, society)
    (output / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    return output
