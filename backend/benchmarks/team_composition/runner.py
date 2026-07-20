"""Runner for Layer A team-composition benchmark attempts."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Protocol

from benchmarks.team_composition.evaluator import evaluate_blocked, evaluate_plan
from benchmarks.team_composition.loader import load_private_label, load_public_scenario
from benchmarks.team_composition.models import (
    BlockedAttemptRecord,
    CompositionEvaluation,
    EvaluatedTrialRecord,
    FailureDetail,
    PublicTrialRecord,
    RunMode,
    relative_path,
)
from benchmarks.team_composition.persistence import make_trial_id, next_trial_index, persist_evaluated_trial, persist_public_trial
from benchmarks.team_composition.reporting import validate_official_freeze
from config import Settings
from society.capability_registry import canonical_tool_id
from society.agents import build_model
from society.provider_preflight import model_capability_preflight
from society.team_composer import AgnoTeamPlanProvider, CompositionContext, TeamComposer, TeamCompositionBlocked, TeamCompositionResult


class ComposerProtocol(Protocol):
    """Minimal protocol for a benchmarkable team composer."""

    async def compose(self, context: CompositionContext) -> TeamCompositionResult:
        """Return a validated composition or raise a typed blocked result."""


_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\b(?:api[_ -]?key|authorization|auth(?:entication)?|token|secret)\b\s*[:=]\s*\S+"),
)
_SECRET_WORD_PATTERN = re.compile(r"(?i)\b(?:api[_ -]?key|authorization|auth(?:entication)?|token|secret)\b")
_USAGE_UNAVAILABLE_REASON = "composer/provider usage metrics not exposed by current team composition surface"


def _normalized_optional_ids(values: list[str] | None, *, canonicalize: bool = False) -> list[str] | None:
    """Return stable optional availability metadata for durable records."""

    if values is None:
        return None
    normalized = [
        canonical_tool_id(value) if canonicalize else value
        for value in values
    ]
    return sorted(dict.fromkeys(normalized))


def _sanitize_failure_message(error: BaseException, *, max_length: int = 240) -> str:
    """Return a bounded, redacted failure message safe for durable records."""

    message = str(error).strip() or error.__class__.__name__
    for pattern in _SECRET_VALUE_PATTERNS:
        message = pattern.sub("[redacted]", message)
    if _SECRET_WORD_PATTERN.search(message):
        message = _SECRET_WORD_PATTERN.sub("[redacted]", message)
    message = " ".join(message.split())
    if len(message) > max_length:
        message = f"{message[: max_length - 3]}..."
    return message


def _build_context_for_scenario(public_scenario: Any) -> CompositionContext:
    fixtures_text = "\n".join(
        f"[{fixture.fixture_id}] {fixture.description}: {fixture.content}"
        for fixture in public_scenario.fixtures
    )
    request = f"{public_scenario.request}\n\nFixtures:\n{fixtures_text}".strip()
    return CompositionContext(
        task_id=public_scenario.scenario_id,
        task_summary=request,
        user_request=request,
        acceptance_requirements=list(public_scenario.acceptance_requirements),
        unresolved_user_requirements=list(public_scenario.unresolved_user_requirements),
        available_tool_ids=(
            list(public_scenario.available_tool_ids)
            if public_scenario.available_tool_ids is not None
            else None
        ),
        available_agent_template_ids=(
            list(public_scenario.available_agent_template_ids)
            if public_scenario.available_agent_template_ids is not None
            else None
        ),
        limits=public_scenario.limits,
        attempt_ceiling=public_scenario.attempt_ceiling,
    )


def _record_runtime_metadata(
    record: PublicTrialRecord,
    *,
    compose_started_at: float,
    usage_surface: Any = None,
) -> None:
    """Persist truthful timing plus explicit usage-unavailable metadata."""

    record.timings = {
        "compose_wall_seconds": round(max(0.0, time.perf_counter() - compose_started_at), 6),
    }
    if isinstance(usage_surface, dict):
        record.usage = dict(usage_surface)
        return
    record.usage = {
        "usage_complete": False,
        "unavailable_reason": _USAGE_UNAVAILABLE_REASON,
    }


async def run_composition_trial(
    *,
    scenario_id: str,
    output_dir: Path,
    mode: RunMode,
    model: str,
    provider: str,
    composer: ComposerProtocol,
    provider_preflight: dict[str, Any] | None = None,
    public_scenario: Any | None = None,
    private_label: Any | None = None,
    trial_id: str | None = None,
) -> EvaluatedTrialRecord:
    """Run one scenario with an injected composer and persist both records."""

    public_scenario = public_scenario or load_public_scenario(scenario_id)
    private_label = private_label or load_private_label(scenario_id)
    context = _build_context_for_scenario(public_scenario)
    if trial_id is None:
        trial_index = next_trial_index(output_dir, scenario_id)
        trial_id = make_trial_id(scenario_id, trial_index)
    public_record = PublicTrialRecord(
        trial_id=trial_id,
        scenario_id=public_scenario.scenario_id,
        scenario_family=public_scenario.family,
        scenario_title=public_scenario.title,
        mode=mode,
        official=mode == "official",
        model=model,
        provider=provider,
        limits=public_scenario.limits,
        attempt_ceiling=public_scenario.attempt_ceiling,
        available_tool_ids=_normalized_optional_ids(context.available_tool_ids, canonicalize=True),
        available_agent_template_ids=_normalized_optional_ids(context.available_agent_template_ids),
        provider_preflight=provider_preflight,
        public_scenario_hash=public_scenario.public_hash,
        public_scenario_ref=f"public_suite:{public_scenario.scenario_id}",
        injected_unavailable_template_id=public_scenario.injected_unavailable_template_id,
    )
    public_path = persist_public_trial(output_dir, public_record)
    compose_started_at = time.perf_counter()
    try:
        result = await composer.compose(context)
        public_record.status = "success"
        public_record.finished_at = public_record.started_at
        public_record.selected_plan = result.plan
        public_record.validation_issue_history = result.validation_issue_history
        public_record.recomposition_count = max(0, result.attempt_count - 1)
        _record_runtime_metadata(
            public_record,
            compose_started_at=compose_started_at,
            usage_surface=getattr(result, "usage", None),
        )
        evaluation = evaluate_plan(
            result.plan,
            private_label,
            available_tool_ids=context.available_tool_ids,
            available_agent_template_ids=context.available_agent_template_ids,
            limits=context.limits,
            validation_issue_history=result.validation_issue_history,
        )
    except TeamCompositionBlocked as blocked:
        public_record.status = "blocked"
        public_record.finished_at = public_record.started_at
        public_record.blocked_result = BlockedAttemptRecord(
            category=blocked.category,
            message=str(blocked),
            issues=blocked.issues,
            pause_for_user=blocked.pause_for_user,
        )
        public_record.validation_issue_history = list(blocked.validation_issue_history)
        public_record.recomposition_count = max(0, blocked.attempt_count - 1)
        _record_runtime_metadata(public_record, compose_started_at=compose_started_at)
        evaluation = evaluate_blocked(blocked, private_label)
    except Exception as exc:
        public_record.status = "failed"
        public_record.finished_at = public_record.started_at
        public_record.failure_detail = FailureDetail(
            error_type=exc.__class__.__name__,
            message=_sanitize_failure_message(exc),
        )
        public_record.failures.append(
            f"{public_record.failure_detail.error_type}: {public_record.failure_detail.message}"
        )
        _record_runtime_metadata(public_record, compose_started_at=compose_started_at)
        evaluation = CompositionEvaluation(
            outcome_type="failed",
            strict_pass=False,
            metrics=[],
            validation_issue_codes=[],
            validation_issue_history=[],
            projected_cost_units=None,
        )
        evaluated = EvaluatedTrialRecord(
            **public_record.model_dump(mode="json"),
            private_label_hash=private_label.private_hash,
            private_label_ref=f"private_labels:{private_label.scenario_id}",
            evaluation=evaluation,
        )
        evaluated.event_log.append({"public_record": relative_path(public_path, output_dir)})
        persist_evaluated_trial(output_dir, evaluated)
        raise

    evaluated = EvaluatedTrialRecord(
        **public_record.model_dump(mode="json"),
        private_label_hash=private_label.private_hash,
        private_label_ref=f"private_labels:{private_label.scenario_id}",
        evaluation=evaluation,
    )
    evaluated.event_log.append({"public_record": relative_path(public_path, output_dir)})
    persist_evaluated_trial(output_dir, evaluated)
    return evaluated


async def run_live_composition_trial(
    *,
    scenario_id: str,
    output_dir: Path,
    mode: RunMode,
    settings: Settings,
    provider_preflight: dict[str, Any] | None = None,
) -> EvaluatedTrialRecord:
    """Run one live scenario through TeamComposer with preflight gating."""

    public_scenario = load_public_scenario(scenario_id)
    private_label = load_private_label(scenario_id)
    trial_index = next_trial_index(output_dir, scenario_id)
    trial_id = make_trial_id(scenario_id, trial_index)
    preflight = provider_preflight or model_capability_preflight(settings)
    context = _build_context_for_scenario(public_scenario)
    validate_official_freeze(
        mode=mode,
        model=settings.active_model,
        provider=settings.provider,
        limits=public_scenario.limits,
        attempt_ceiling=public_scenario.attempt_ceiling,
        available_tool_ids=_normalized_optional_ids(context.available_tool_ids, canonicalize=True),
        available_agent_template_ids=_normalized_optional_ids(context.available_agent_template_ids),
        suite_version=PublicTrialRecord.model_fields["suite_version"].default,
        evaluator_version=CompositionEvaluation.model_fields["evaluator_version"].default,
        provider_preflight=preflight,
        scenario=public_scenario,
        scenario_family=public_scenario.family,
        scenario_title=public_scenario.title,
        public_scenario_hash=public_scenario.public_hash,
        public_scenario_ref=f"public_suite:{public_scenario.scenario_id}",
        injected_unavailable_template_id=public_scenario.injected_unavailable_template_id,
    )
    if preflight.get("typed_blockers"):
        public_record = PublicTrialRecord(
            trial_id=trial_id,
            scenario_id=public_scenario.scenario_id,
            scenario_family=public_scenario.family,
            scenario_title=public_scenario.title,
            mode=mode,
            official=False,
            model=settings.active_model,
            provider=settings.provider,
            limits=public_scenario.limits,
            attempt_ceiling=public_scenario.attempt_ceiling,
            available_tool_ids=_normalized_optional_ids(context.available_tool_ids, canonicalize=True),
            available_agent_template_ids=_normalized_optional_ids(context.available_agent_template_ids),
            provider_preflight=preflight,
            public_scenario_hash=public_scenario.public_hash,
            public_scenario_ref=f"public_suite:{public_scenario.scenario_id}",
            injected_unavailable_template_id=public_scenario.injected_unavailable_template_id,
            exclusions=[blocker["code"] for blocker in preflight.get("typed_blockers", [])],
        )
        persist_public_trial(output_dir, public_record)
        blocked = TeamCompositionBlocked(
            category="missing_system_capability",
            message="provider preflight reported typed blockers",
            issues=[],
            pause_for_user=True,
        )
        public_record.status = "excluded"
        public_record.blocked_result = BlockedAttemptRecord(category=blocked.category, message=str(blocked), issues=[], pause_for_user=True)
        evaluation = evaluate_blocked(blocked, private_label).model_copy(update={"outcome_type": "excluded"})
        evaluated = EvaluatedTrialRecord(
            **public_record.model_dump(mode="json"),
            private_label_hash=private_label.private_hash,
            private_label_ref=f"private_labels:{private_label.scenario_id}",
            evaluation=evaluation,
        )
        evaluated.event_log.append({
            "public_record": relative_path(
                output_dir / public_record.scenario_id / f"{public_record.trial_id}.public.json",
                output_dir,
            )
        })
        persist_evaluated_trial(output_dir, evaluated)
        return evaluated

    context = _build_context_for_scenario(public_scenario)
    provider = AgnoTeamPlanProvider(model=build_model(settings))
    composer = TeamComposer(provider)
    return await run_composition_trial(
        scenario_id=scenario_id,
        output_dir=output_dir,
        mode=mode,
        model=settings.active_model,
        provider=settings.provider,
        composer=composer,
        provider_preflight=preflight,
        public_scenario=public_scenario,
        private_label=private_label,
        trial_id=trial_id,
    )


def run_trial_sync(**kwargs: Any) -> EvaluatedTrialRecord:
    """Synchronous wrapper for test and CLI callers."""

    return asyncio.run(run_composition_trial(**kwargs))
