"""Deterministic aggregation and report generation for benchmark trials."""
from __future__ import annotations
import json, statistics
from pathlib import Path
from typing import Any
from benchmarks.runtime import TrialResult

def aggregate_trials(trials: list[TrialResult]) -> dict[str, Any]:
    """Aggregate attempts without dropping failures or incomplete usage."""
    successes = [t for t in trials if t.status == "success" and t.evaluation]
    scores = [t.evaluation.total_score for t in successes if t.evaluation]
    durations = [t.wall_duration_s for t in successes]
    complete = bool(successes) and all(t.usage.usage_complete for t in successes)
    costs_complete = bool(successes) and all(t.usage.cost is not None for t in successes)
    tokens = sum(t.usage.total_tokens or 0 for t in successes)
    minutes = sum(durations) / 60.0
    return {"attempts": len(trials), "successes": len(successes), "failures": len(trials)-len(successes),
        "failure_rate": (len(trials)-len(successes))/len(trials) if trials else 0.0,
        "mean_score": statistics.mean(scores) if scores else None,
        "min_score": min(scores) if scores else None, "max_score": max(scores) if scores else None,
        "score_stddev": statistics.pstdev(scores) if len(scores)>1 else 0.0 if scores else None,
        "mean_duration_seconds": statistics.mean(durations) if durations else None,
        "model_calls": sum(t.usage.model_calls or 0 for t in successes) if successes else None,
        "total_tokens": tokens if complete else None,
        "total_cost": sum(t.usage.cost for t in successes if t.usage.cost is not None) if costs_complete else None,
        "cost_complete": costs_complete,
        "usage_complete": complete,
        "score_per_minute": sum(scores)/minutes if scores and minutes>0 else None,
        "score_per_million_tokens": sum(scores)/(tokens/1_000_000) if scores and complete and tokens>0 else None}

def compare_modes(single: list[TrialResult], society: list[TrialResult]) -> dict[str, Any]:
    """Compare modes, requiring three successes per mode before a verdict."""
    left, right = aggregate_trials(single), aggregate_trials(society)
    delta = right["mean_score"]-left["mean_score"] if left["mean_score"] is not None and right["mean_score"] is not None else None
    enough_data = left["successes"] >= 3 and right["successes"] >= 3
    verdict = "insufficient_data" if delta is None or not enough_data else ("society_quality_win" if delta>=5 else "single_agent_quality_win" if delta<=-5 else "no_clear_quality_winner")
    return {"single_agent": left, "society": right, "quality_delta": delta, "verdict": verdict}

def write_bundle(output_dir: Path, single: list[TrialResult], society: list[TrialResult]) -> Path:
    """Persist every raw trial plus comparison JSON and Markdown."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for mode, trials in (("single_agent", single), ("society", society)):
        folder=output_dir/mode; folder.mkdir(exist_ok=True)
        for i, trial in enumerate(trials,1):
            (folder/f"trial-{i:02d}.json").write_text(trial.model_dump_json(indent=2),encoding="utf-8")
    comparison=compare_modes(single,society)
    (output_dir/"comparison.json").write_text(json.dumps(comparison,indent=2),encoding="utf-8")
    lines=[
        "# Qwendom Benchmark Report", "",
        f"Verdict: **{comparison['verdict']}**", "",
        f"Quality delta (society - single agent): {comparison['quality_delta']}", "",
    ]
    for mode in ("single_agent","society"):
        d=comparison[mode]; lines += [
            f"## {mode.replace('_',' ').title()}", "",
            f"- Attempts: {d['attempts']}",
            f"- Successes: {d['successes']}",
            f"- Failure rate: {d['failure_rate']:.1%}",
            f"- Mean score: {d['mean_score']}",
            f"- Score range: {d['min_score']} to {d['max_score']}",
            f"- Mean wall time: {d['mean_duration_seconds']} seconds",
            f"- Model calls: {d['model_calls']}",
            f"- Total tokens: {d['total_tokens']}",
            f"- Provider cost: {d['total_cost'] if d['cost_complete'] else 'unavailable'}",
            f"- Score per minute: {d['score_per_minute']}",
            f"- Score per million tokens: {d['score_per_million_tokens']}", "",
        ]
    (output_dir/"REPORT.md").write_text("\n".join(lines),encoding="utf-8")
    return output_dir
