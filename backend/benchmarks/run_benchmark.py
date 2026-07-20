"""Run the failure-preserving Qwen benchmark under one shared budget."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.loader import build_prompt
from benchmarks.reporting import write_bundle
from benchmarks.runtime import TrialResult, hash_text
from benchmarks.single_agent import run_single_agent
from benchmarks.society import run_society
from config import get_settings

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_TOKEN_BUDGET = 1_500_000
SYNTHESIS_RESERVE_SECONDS = 120.0


async def _collect(
    mode: str,
    target: int,
    max_attempts: int,
    timeout_seconds: float,
    token_budget: int,
) -> list[TrialResult]:
    """Collect successful trials while retaining every failed attempt."""
    settings = get_settings()
    trials: list[TrialResult] = []
    while sum(t.status == "success" for t in trials) < target and len(trials) < max_attempts:
        if mode == "single_agent":
            trial = await run_single_agent(
                settings,
                timeout_s=timeout_seconds,
                max_total_tokens=token_budget,
            )
        else:
            trial = await run_society(
                settings,
                society_timeout_s=max(1.0, timeout_seconds - SYNTHESIS_RESERVE_SECONDS),
                synthesis_timeout_s=min(SYNTHESIS_RESERVE_SECONDS, timeout_seconds),
                max_total_tokens=token_budget,
            )
        trials.append(trial)
    return trials


async def main() -> int:
    """Parse CLI arguments, run requested modes, and write the evidence bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--successful-trials", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument("--mode", choices=("both", "single_agent", "society"), default="both")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    output = args.output or Path("benchmark_results") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-qwen37plus"
    )
    single = await _collect(
        "single_agent", args.successful_trials, args.max_attempts,
        args.timeout_seconds, args.token_budget,
    ) if args.mode in {"both", "single_agent"} else []
    society = await _collect(
        "society", args.successful_trials, args.max_attempts,
        args.timeout_seconds, args.token_budget,
    ) if args.mode in {"both", "society"} else []

    write_bundle(output, single, society)
    config = {
        "provider": get_settings().provider,
        "model": get_settings().active_model,
        "prompt_hash": hash_text(build_prompt("single_agent")),
        "successful_trials_target": args.successful_trials,
        "max_attempts": args.max_attempts,
        "timeout_seconds_per_trial": args.timeout_seconds,
        "max_total_tokens_per_trial": args.token_budget,
        "external_research": "disabled_for_both_modes",
        "evaluator": "deterministic_no_llm_judge",
    }
    (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return 0 if all(t.status == "success" for t in [*single, *society]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
