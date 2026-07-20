"""Run the frozen multi-ask qwen3.7-plus benchmark suite v2."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.reporting_v2 import write_bundle
from benchmarks.loader_v2 import build_task_prompt
from benchmarks.runtime import hash_text
from benchmarks.runners_v2 import run_single_task, run_society_task
from benchmarks.suite_v2 import TASKS
from config import Settings, get_settings


async def _collect(
    settings: Settings, mode: str, task_ids: list[str], successes: int,
    max_attempts: int, timeout: float, budget: int, task_concurrency: int,
):
    semaphore = asyncio.Semaphore(max(1, task_concurrency))

    async def collect_task(task_id: str):
        task_trials = []
        task_root = Path(settings.event_store_file).parent / "runtime" / task_id
        task_settings = settings.model_copy(update={
            "event_store_file": str((task_root / "events.jsonl").resolve()),
            "agno_sqlite_file": str((task_root / "agno.sqlite").resolve()),
        })
        async with semaphore:
            while sum(t.status == "success" for t in task_trials) < successes and len(task_trials) < max_attempts:
                if mode == "single_agent":
                    trial = await run_single_task(task_settings, task_id, timeout_s=timeout, max_total_tokens=budget)
                else:
                    trial = await run_society_task(
                        task_settings, task_id, society_timeout_s=max(1, timeout - 120),
                        synthesis_timeout_s=min(120, timeout), max_total_tokens=budget,
                    )
                task_trials.append(trial)
        return task_trials

    grouped = await asyncio.gather(*(collect_task(task_id) for task_id in task_ids))
    return [trial for task_trials in grouped for trial in task_trials]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--successful-trials", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--token-budget", type=int, default=1_500_000)
    parser.add_argument("--mode", choices=("both", "single_agent", "society"), default="both")
    parser.add_argument("--task-id", action="append", choices=tuple(TASKS), dest="task_ids")
    parser.add_argument("--task-concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    task_ids = args.task_ids or list(TASKS)
    output = args.output or Path("benchmark_results") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-suite-v2"
    )
    settings = get_settings().model_copy(update={
        "event_store_file": str((output / "events.jsonl").resolve()),
        "agno_sqlite_file": str((output / "agno.sqlite").resolve()),
    })
    single = await _collect(settings, "single_agent", task_ids, args.successful_trials, args.max_attempts, args.timeout_seconds, args.token_budget, args.task_concurrency) if args.mode in {"both", "single_agent"} else []
    society = await _collect(settings, "society", task_ids, args.successful_trials, args.max_attempts, args.timeout_seconds, args.token_budget, args.task_concurrency) if args.mode in {"both", "society"} else []
    write_bundle(output, single, society)
    config = {
        "suite_version": "v2", "task_ids": task_ids, "provider": settings.provider,
        "model": settings.active_model, "shared_tools": ["lookup_record", "lookup_dataset", "calculate"],
        "event_store": "isolated_per_task_in_result_bundle",
        "agno_db": "isolated_per_task_in_result_bundle",
        "prompt_hashes": {task_id: hash_text(build_task_prompt(task_id)) for task_id in TASKS},
        "suite_hash": hash_text("\n".join(build_task_prompt(task_id) for task_id in TASKS)),
        "successful_trials_per_task": args.successful_trials, "max_attempts_per_task": args.max_attempts,
        "task_concurrency": args.task_concurrency,
        "timeout_seconds": args.timeout_seconds, "token_budget_per_trial": args.token_budget,
        "external_research": "disabled", "evaluator": "deterministic_no_llm_judge",
    }
    (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    expected = len(task_ids) * args.successful_trials
    return 0 if len([t for t in single if t.status == "success"]) in {0, expected} and len([t for t in society if t.status == "success"]) in {0, expected} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
