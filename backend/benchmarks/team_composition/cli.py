"""Narrow CLI entrypoint for the Layer A team-composition benchmark."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from benchmarks.team_composition.public_suite import load_public_scenarios
from benchmarks.team_composition.reporting import write_aggregate_bundle
from benchmarks.team_composition.runner import run_live_composition_trial
from config import get_settings
from society.provider_preflight import model_capability_preflight


def build_parser() -> argparse.ArgumentParser:
    """Build the narrow benchmark CLI parser."""

    parser = argparse.ArgumentParser(description="Run the Layer A team-composition benchmark.")
    parser.add_argument("--scenario", default="all", help="Scenario ID or 'all'.")
    parser.add_argument("--mode", choices=["development", "official"], default="development")
    parser.add_argument("--output-dir", default="backend/benchmark_results/team_composition")
    return parser


def _prepare_output_dir(*, mode: str, scenario: str, output_dir: Path) -> Path:
    """Refuse official collections that could silently mix old retained trials."""

    if mode != "official":
        return output_dir
    if scenario != "all":
        raise ValueError("official mode refused because the full public suite must run exactly once")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("official mode refused because output directory must be empty")
    return output_dir


async def _run(args: argparse.Namespace) -> None:
    output_dir = _prepare_output_dir(
        mode=args.mode,
        scenario=args.scenario,
        output_dir=Path(args.output_dir),
    )
    scenarios = load_public_scenarios()
    selected_ids = [args.scenario] if args.scenario != "all" else [scenario.scenario_id for scenario in scenarios]
    settings = get_settings()
    provider_preflight = model_capability_preflight(settings)
    for scenario_id in selected_ids:
        await run_live_composition_trial(
            scenario_id=scenario_id,
            output_dir=output_dir,
            mode=args.mode,
            settings=settings,
            provider_preflight=provider_preflight,
        )
    write_aggregate_bundle(output_dir, mode=args.mode, provider_preflight=provider_preflight)


def main() -> None:
    """Run the benchmark CLI."""

    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
