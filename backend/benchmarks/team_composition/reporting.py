"""Aggregate reporting for the Layer A team-composition benchmark."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarks.team_composition.loader import load_public_suite
from benchmarks.team_composition.models import (
    AggregateReport,
    AggregateScenarioReport,
    EvaluatedTrialRecord,
    FROZEN_OFFICIAL_CONFIG,
    OfficialFreeze,
    PublicScenario,
    RunMode,
    canonical_json,
)
from benchmarks.team_composition.persistence import load_evaluated_trials


def _stable_optional_list(values: list[str] | None) -> list[str] | None:
    """Return a normalized optional list for official fairness comparisons."""

    if values is None:
        return None
    return sorted(dict.fromkeys(values))


def _scenario_freeze_metadata(scenario: PublicScenario) -> dict[str, Any]:
    """Return the sealed public metadata that official trials must preserve."""

    return {
        "scenario_id": scenario.scenario_id,
        "scenario_family": scenario.family,
        "scenario_title": scenario.title,
        "limits": scenario.limits.model_dump(mode="json"),
        "attempt_ceiling": scenario.attempt_ceiling,
        "available_tool_ids": _stable_optional_list(scenario.available_tool_ids),
        "available_agent_template_ids": _stable_optional_list(scenario.available_agent_template_ids),
        "public_scenario_hash": scenario.public_hash,
        "public_scenario_ref": f"public_suite:{scenario.scenario_id}",
        "injected_unavailable_template_id": scenario.injected_unavailable_template_id,
    }


def _trial_freeze_metadata(trial: EvaluatedTrialRecord) -> dict[str, Any]:
    """Return the persisted public metadata carried by one retained trial."""

    return {
        "scenario_id": trial.scenario_id,
        "scenario_family": trial.scenario_family,
        "scenario_title": trial.scenario_title,
        "limits": trial.limits.model_dump(mode="json"),
        "attempt_ceiling": trial.attempt_ceiling,
        "available_tool_ids": _stable_optional_list(trial.available_tool_ids),
        "available_agent_template_ids": _stable_optional_list(trial.available_agent_template_ids),
        "public_scenario_hash": trial.public_scenario_hash,
        "public_scenario_ref": trial.public_scenario_ref,
        "injected_unavailable_template_id": trial.injected_unavailable_template_id,
    }


def validate_official_freeze(
    *,
    mode: RunMode,
    model: str,
    provider: str,
    limits: Any,
    attempt_ceiling: int,
    available_tool_ids: list[str] | None,
    available_agent_template_ids: list[str] | None,
    suite_version: str,
    evaluator_version: str,
    provider_preflight: dict[str, Any] | None,
    scenario: PublicScenario | None = None,
    scenario_family: str | None = None,
    scenario_title: str | None = None,
    public_scenario_hash: str | None = None,
    public_scenario_ref: str | None = None,
    injected_unavailable_template_id: str | None = None,
    freeze: OfficialFreeze = FROZEN_OFFICIAL_CONFIG,
) -> None:
    """Refuse official mode when the run is not using the frozen config."""

    if mode != "official":
        return
    if provider_preflight is None:
        raise ValueError("official mode refused because provider preflight is required")
    blockers = list((provider_preflight or {}).get("typed_blockers", []))
    if blockers:
        raise ValueError("official mode refused because provider preflight has typed blockers")
    if suite_version != freeze.suite_version:
        raise ValueError("official mode refused because suite version is not frozen")
    if evaluator_version != freeze.evaluator_version:
        raise ValueError("official mode refused because evaluator version is not frozen")
    if provider != freeze.provider:
        raise ValueError("official mode refused because provider is not the frozen accepted provider")
    if model != freeze.model:
        raise ValueError("official mode refused because model is not the frozen accepted model")
    if attempt_ceiling != freeze.attempt_ceiling:
        raise ValueError("official mode refused because attempt ceiling differs from the frozen accepted value")
    if scenario is None:
        return
    expected = _scenario_freeze_metadata(scenario)
    actual = {
        "scenario_id": scenario.scenario_id,
        "scenario_family": scenario_family,
        "scenario_title": scenario_title,
        "limits": limits.model_dump(mode="json"),
        "attempt_ceiling": attempt_ceiling,
        "available_tool_ids": _stable_optional_list(available_tool_ids),
        "available_agent_template_ids": _stable_optional_list(available_agent_template_ids),
        "public_scenario_hash": public_scenario_hash,
        "public_scenario_ref": public_scenario_ref,
        "injected_unavailable_template_id": injected_unavailable_template_id,
    }
    if actual != expected:
        raise ValueError("official mode refused because scenario metadata differs from the frozen public suite")


def _validate_official_trial_consistency(
    trials: list[EvaluatedTrialRecord],
    *,
    scenarios: list[PublicScenario],
    provider_preflight: dict[str, Any],
) -> None:
    """Reject official aggregates that do not match the sealed suite exactly once."""

    official_scenarios = {
        scenario.scenario_id: scenario
        for scenario in scenarios
        if scenario.official
    }
    official_trial_counts: dict[str, int] = defaultdict(int)
    for trial in trials:
        if trial.status == "excluded":
            raise ValueError("official aggregate refused because excluded trials are not official collection evidence")
        if trial.mode != "official" or not trial.official:
            raise ValueError("official aggregate refused because retained trials must all be official")
        scenario = official_scenarios.get(trial.scenario_id)
        if scenario is None:
            raise ValueError("official aggregate refused because retained trials include a non-official or unknown scenario")
        validate_official_freeze(
            mode=trial.mode,
            model=trial.model,
            provider=trial.provider,
            limits=trial.limits,
            attempt_ceiling=trial.attempt_ceiling,
            available_tool_ids=trial.available_tool_ids,
            available_agent_template_ids=trial.available_agent_template_ids,
            suite_version=trial.suite_version,
            evaluator_version=trial.evaluation.evaluator_version,
            provider_preflight=trial.provider_preflight,
            scenario=scenario,
            scenario_family=trial.scenario_family,
            scenario_title=trial.scenario_title,
            public_scenario_hash=trial.public_scenario_hash,
            public_scenario_ref=trial.public_scenario_ref,
            injected_unavailable_template_id=trial.injected_unavailable_template_id,
        )
        if trial.provider_preflight != provider_preflight:
            raise ValueError("official aggregate refused because retained trials do not share identical provider preflight evidence")
        official_trial_counts[trial.scenario_id] += 1

    duplicate_ids = sorted(
        scenario_id
        for scenario_id, count in official_trial_counts.items()
        if count != 1
    )
    if duplicate_ids:
        raise ValueError(
            "official aggregate refused because each official scenario needs exactly one retained trial: "
            + ", ".join(duplicate_ids)
        )
    missing_ids = sorted(set(official_scenarios) - set(official_trial_counts))
    if missing_ids:
        raise ValueError(
            "official aggregate refused because official scenarios are missing retained trials: "
            + ", ".join(missing_ids)
        )


def aggregate_trials(
    trials: list[EvaluatedTrialRecord],
    *,
    mode: RunMode,
    provider_preflight: dict[str, Any] | None = None,
) -> AggregateReport:
    """Aggregate scenario metrics without hiding failed or excluded attempts."""

    scenarios, public_suite_hash = load_public_suite()
    scenario_family = {scenario.scenario_id: scenario.family for scenario in scenarios}
    scenario_official = {scenario.scenario_id: scenario.official for scenario in scenarios}
    if not trials:
        raise ValueError("no evaluated trials were provided")
    head = trials[0]
    if mode == "official":
        _validate_official_trial_consistency(
            trials,
            scenarios=scenarios,
            provider_preflight=provider_preflight or {},
        )
    grouped: dict[str, list[EvaluatedTrialRecord]] = defaultdict(list)
    for trial in trials:
        grouped[trial.scenario_id].append(trial)

    reports: list[AggregateScenarioReport] = []
    for scenario_id in sorted(grouped):
        scenario_trials = grouped[scenario_id]
        metric_values: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trial in scenario_trials:
            for metric in trial.evaluation.metrics:
                metric_values[metric.name].append({
                    "passed": metric.passed,
                    "value": metric.value,
                    "detail": metric.detail,
                })
        reports.append(
            AggregateScenarioReport(
                scenario_id=scenario_id,
                family=scenario_family.get(scenario_id, ""),
                official=scenario_official.get(scenario_id, False),
                attempts=len(scenario_trials),
                successes=sum(1 for trial in scenario_trials if trial.status == "success"),
                blocked=sum(1 for trial in scenario_trials if trial.status == "blocked"),
                failed=sum(1 for trial in scenario_trials if trial.status == "failed"),
                excluded=sum(1 for trial in scenario_trials if trial.status == "excluded"),
                strict_passes=sum(1 for trial in scenario_trials if trial.evaluation.strict_pass),
                metrics={
                    name: {
                        "passes": sum(1 for value in values if value["passed"]),
                        "attempts": len(values),
                        "values": values,
                    }
                    for name, values in sorted(metric_values.items())
                },
            )
        )

    return AggregateReport(
        mode=mode,
        official=mode == "official",
        model=head.model,
        provider=head.provider,
        limits=head.limits,
        attempt_ceiling=head.attempt_ceiling,
        public_suite_hash=public_suite_hash,
        scenario_reports=reports,
        retained_trial_files=sorted(trial.trial_id for trial in trials),
        provider_preflight_blockers=list((provider_preflight or {}).get("typed_blockers", [])),
    )


def render_markdown_report(report: AggregateReport) -> str:
    """Render a concise scenario-visible Markdown aggregate."""

    lines = [
        f"# Team Composition Benchmark ({report.mode})",
        "",
        f"- suite_version: `{report.suite_version}`",
        f"- evaluator_version: `{report.evaluator_version}`",
        f"- model: `{report.model}`",
        f"- provider: `{report.provider}`",
        f"- official: `{str(report.official).lower()}`",
        "",
    ]
    for scenario in report.scenario_reports:
        lines.extend([
            f"## {scenario.scenario_id}",
            "",
            f"- family: `{scenario.family}`",
            f"- attempts: `{scenario.attempts}`",
            f"- successes: `{scenario.successes}`",
            f"- blocked: `{scenario.blocked}`",
            f"- failed: `{scenario.failed}`",
            f"- excluded: `{scenario.excluded}`",
            f"- strict_passes: `{scenario.strict_passes}`",
        ])
        for metric_name, metric_summary in scenario.metrics.items():
            lines.append(f"- {metric_name}: `{metric_summary['passes']}/{metric_summary['attempts']}` passes")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_aggregate_bundle(
    output_dir: Path,
    *,
    mode: RunMode,
    provider_preflight: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Load persisted trials, build aggregate JSON/Markdown, and write both."""

    trials = load_evaluated_trials(output_dir)
    report = aggregate_trials(trials, mode=mode, provider_preflight=provider_preflight)
    json_path = output_dir / f"aggregate-{mode}.json"
    md_path = output_dir / f"aggregate-{mode}.md"
    json_path.write_text(canonical_json(report), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return json_path, md_path
