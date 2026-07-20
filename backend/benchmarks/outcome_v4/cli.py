"""CLI for deterministic and provider-backed Layer B validation runs."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .fixtures import list_public_scenarios
from .runner import (
    DEFAULT_DEVELOPMENT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    evaluate_official_gates,
    run_attempt,
)


def _timeout_seconds_arg(raw: str) -> float:
    """Validate one bounded CLI timeout value."""

    value = float(raw)
    if value < MIN_TIMEOUT_SECONDS or value > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"timeout must be between {MIN_TIMEOUT_SECONDS:.0f} and {MAX_TIMEOUT_SECONDS:.0f} seconds"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the narrow Layer B CLI."""

    parser = argparse.ArgumentParser(
        description=(
            "Layer B outcome benchmark harness. Safe starting points:\n"
            "  python -m benchmarks.outcome_v4.cli validate\n"
            "  python -m benchmarks.outcome_v4.cli run --scenario incident_repair --benchmark-mode single_agent --run-mode development\n"
            "  python -m benchmarks.outcome_v4.cli run --scenario screenshot_to_product --benchmark-mode society --run-mode development"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate public fixtures and official refusal gates.")
    validate_parser.add_argument("--scenario", choices=["all", "incident_repair", "screenshot_to_product"], default="all")

    run_parser = subparsers.add_parser("run", help="Run one development attempt or show official refusal.")
    run_parser.add_argument("--scenario", choices=["incident_repair", "screenshot_to_product"], required=True)
    run_parser.add_argument("--benchmark-mode", choices=["single_agent", "society"], required=True)
    run_parser.add_argument("--run-mode", choices=["development", "official"], default="development")
    run_parser.add_argument("--adapter", choices=["deterministic", "provider"], default="deterministic")
    run_parser.add_argument("--output-dir", default="backend/benchmark_results/outcome_v4")
    run_parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds_arg,
        default=DEFAULT_DEVELOPMENT_TIMEOUT_SECONDS,
        help=(
            "Mode-neutral development timeout shared across both benchmark modes and scenarios. "
            f"Default: {DEFAULT_DEVELOPMENT_TIMEOUT_SECONDS:.0f}s."
        ),
    )
    return parser


async def _run_validate(args: argparse.Namespace) -> int:
    scenario_ids = (
        [scenario.scenario_id for scenario in list_public_scenarios()]
        if args.scenario == "all"
        else [args.scenario]
    )
    lines: list[str] = []
    for scenario_id in scenario_ids:
        gates = evaluate_official_gates(scenario_id, deterministic=True)
        lines.append(f"{scenario_id}: public fixture ready for development, official ready={gates.ready}")
        for reason in gates.reasons:
            lines.append(f"  - {reason}")
    print("\n".join(lines))
    return 0


async def _run_command(args: argparse.Namespace) -> int:
    record = await run_attempt(
        scenario_id=args.scenario,
        benchmark_mode=args.benchmark_mode,
        run_mode=args.run_mode,
        output_dir=Path(args.output_dir),
        adapter=args.adapter,
        timeout_seconds=args.timeout_seconds,
    )
    if record.status == "refused":
        refusal_label = (
            "official run refused"
            if args.run_mode == "official"
            else "development/provider run refused"
        )
        print(f"{refusal_label} for {record.scenario_id}/{record.benchmark_mode}")
        for reason in record.refusal_reasons:
            print(f"- {reason}")
        return 2
    print(f"{record.status}: {record.attempt_id}")
    print(f"artifact dir: {args.output_dir}/{record.attempt_id}")
    return 0 if record.status == "success" else 1


def main() -> None:
    """Run the Layer B CLI."""

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "validate":
        raise SystemExit(asyncio.run(_run_validate(args)))
    raise SystemExit(asyncio.run(_run_command(args)))


if __name__ == "__main__":
    main()
