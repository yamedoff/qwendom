"""Run the frozen qwen3.7-plus security-audit benchmark suite v3."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.reporting_v3 import write_bundle
from benchmarks.runners_v3 import (
    MAX_TOOL_CALLS, build_run_prompt, run_single_task, run_society_task,
)
from benchmarks.runtime import hash_text
from benchmarks.tools_v3 import TOOL_NAMES
from config import Settings, get_settings


async def _collect(
    settings: Settings,
    mode: str,
    successes: int,
    max_attempts: int,
    timeout: float,
    budget: int,
) -> list:
    """Collect the requested successful trials while preserving failed attempts."""

    trials = []
    while sum(trial.status == "success" for trial in trials) < successes and len(trials) < max_attempts:
        if mode == "single_agent":
            trial = await run_single_task(
                settings, timeout_s=timeout, max_total_tokens=budget
            )
        else:
            trial = await run_society_task(
                settings,
                society_timeout_s=max(1.0, timeout - 180.0),
                synthesis_timeout_s=min(180.0, timeout),
                max_total_tokens=budget,
            )
        trials.append(trial)
    return trials


async def main() -> int:
    """Parse CLI options, run isolated modes, and persist configuration evidence."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--successful-trials", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--token-budget", type=int, default=1_500_000)
    parser.add_argument("--mode", choices=("both", "single_agent", "society"), default="both")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("benchmark_results") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-suite-v3"
    )
    settings = get_settings().model_copy(update={
        "event_store_file": str((output / "events.jsonl").resolve()),
        "agno_sqlite_file": str((output / "agno.sqlite").resolve()),
    })

    mode_settings: dict[str, Settings] = {}
    for mode in ("single_agent", "society"):
        runtime = output / "runtime" / mode
        mode_settings[mode] = settings.model_copy(update={
            "event_store_file": str((runtime / "events.jsonl").resolve()),
            "agno_sqlite_file": str((runtime / "agno.sqlite").resolve()),
        })
    single = await _collect(
        mode_settings["single_agent"], "single_agent", args.successful_trials,
        args.max_attempts, args.timeout_seconds, args.token_budget,
    ) if args.mode in {"both", "single_agent"} else []
    society = await _collect(
        mode_settings["society"], "society", args.successful_trials,
        args.max_attempts, args.timeout_seconds, args.token_budget,
    ) if args.mode in {"both", "society"} else []
    write_bundle(output, single, society)
    prompt, _ = build_run_prompt()
    config = {
        "suite_version": "v3",
        "task_id": "security-release-audit",
        "provider": settings.provider,
        "model": settings.active_model,
        "shared_tools": list(TOOL_NAMES),
        "tool_call_budget_per_trial": MAX_TOOL_CALLS,
        "event_store": "isolated_per_mode_in_result_bundle",
        "agno_db": "isolated_per_mode_in_result_bundle",
        "prompt_hash": hash_text(prompt),
        "suite_hash": hash_text(prompt),
        "successful_trials_per_mode": args.successful_trials,
        "max_attempts_per_mode": args.max_attempts,
        "timeout_seconds": args.timeout_seconds,
        "token_budget_per_trial": args.token_budget,
        "external_research": "disabled",
        "evaluator": "24_deterministic_acceptance_checks_no_llm_judge",
        "primary_efficiency_metrics": [
            "acceptance_checks_per_minute", "acceptance_checks_per_million_tokens"
        ],
    }
    (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    expected = args.successful_trials
    single_successes = sum(trial.status == "success" for trial in single)
    society_successes = sum(trial.status == "success" for trial in society)
    single_complete = args.mode == "society" or single_successes == expected
    society_complete = args.mode == "single_agent" or society_successes == expected
    return 0 if single_complete and society_complete else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
