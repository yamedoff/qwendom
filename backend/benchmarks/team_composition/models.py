"""Typed models for the sealed Layer A team-composition benchmark.

The benchmark separates the public scenario surface from the private labels
used during evaluation. Public records can be persisted before evaluation
without exposing private expectations to providers or event streams.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from society.schemas.team_composition import CompositionLimits, PlanValidationIssue, TeamCompositionPlan

RunMode = Literal["development", "official"]
AttemptStatus = Literal["success", "blocked", "failed", "excluded"]

SUITE_VERSION = "team-composition-layer-a-2026-07-14"
EVALUATOR_VERSION = "1.0.0"


def canonical_json(value: Any) -> str:
    """Return a deterministic JSON string for hashing and persistence."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude_none=True)
    else:
        payload = value
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)


def stable_hash(value: Any) -> str:
    """Return a deterministic SHA-256 digest for *value*."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    """Return the current UTC time in a stable ISO-8601 format."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class PublicFixture(BaseModel):
    """A public fixture reference visible to the composer."""

    fixture_id: str
    description: str
    content: str


class PublicScenario(BaseModel):
    """A sealed public scenario visible to the benchmarked composer."""

    scenario_id: str
    family: str
    title: str
    request: str
    fixtures: list[PublicFixture] = Field(default_factory=list)
    acceptance_requirements: list[str] = Field(default_factory=list)
    unresolved_user_requirements: list[str] = Field(default_factory=list)
    limits: CompositionLimits = Field(default_factory=CompositionLimits)
    attempt_ceiling: int = Field(default=2, ge=1)
    available_agent_template_ids: list[str] | None = None
    available_tool_ids: list[str] | None = None
    injected_unavailable_template_id: str | None = None
    official: bool = True

    @property
    def public_hash(self) -> str:
        """Return the stable content hash for the public scenario surface."""

        return stable_hash(self)


class ValidatorRequirement(BaseModel):
    """Private requirement for independent artifact validation."""

    target_template_ids: list[str] = Field(default_factory=list)
    allowed_validator_template_ids: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)


class DependencyEdgeLabel(BaseModel):
    """Private dependency rule expressed in template-level terms."""

    producer_template_id: str
    consumer_template_id: str


class RecoveryExpectation(BaseModel):
    """Private expectation for one injected unavailable specialist."""

    unavailable_template_id: str
    recoverable: bool = True
    required_capabilities_after_recovery: list[str] = Field(default_factory=list)
    forbidden_selected_template_ids: list[str] = Field(default_factory=list)
    expected_blocker_category: str | None = None


class TeamSizeExpectation(BaseModel):
    """Private efficiency expectation for selected team size."""

    min_assignments: int = Field(default=0, ge=0)
    max_assignments: int = Field(default=5, ge=0)
    max_projected_cost_units: int = Field(default=15, ge=0)


class PrivateScenarioLabel(BaseModel):
    """Private label set used only by the evaluator after execution."""

    scenario_id: str
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    forbidden_capabilities: list[str] = Field(default_factory=list)
    forbidden_tool_grants: list[str] = Field(default_factory=list)
    required_validator_requirements: list[ValidatorRequirement] = Field(default_factory=list)
    required_dependency_edges: list[DependencyEdgeLabel] = Field(default_factory=list)
    allowed_dependency_edges: list[DependencyEdgeLabel] = Field(default_factory=list)
    forbidden_parallel_template_pairs: list[tuple[str, str]] = Field(default_factory=list)
    expected_blocker_category: str | None = None
    team_size_expectation: TeamSizeExpectation = Field(default_factory=TeamSizeExpectation)
    recovery_expectation: RecoveryExpectation | None = None

    @property
    def private_hash(self) -> str:
        """Return the stable content hash for the private label set."""

        return stable_hash(self)


class MetricResult(BaseModel):
    """One explicit benchmark metric with raw detail preserved."""

    name: str
    passed: bool
    value: float | int | bool | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CompositionEvaluation(BaseModel):
    """Structured post-evaluation record for one attempt."""

    evaluator_version: str = EVALUATOR_VERSION
    outcome_type: Literal["plan", "blocked", "failed", "excluded"]
    strict_pass: bool = False
    metrics: list[MetricResult] = Field(default_factory=list)
    validation_issue_codes: list[str] = Field(default_factory=list)
    validation_issue_history: list[list[str]] = Field(default_factory=list)
    projected_cost_units: int | None = None


class BlockedAttemptRecord(BaseModel):
    """Truthful blocked result retained without converting it into a plan."""

    category: str
    message: str
    issues: list[PlanValidationIssue] = Field(default_factory=list)
    pause_for_user: bool = True


class FailureDetail(BaseModel):
    """Sanitized failure evidence retained for unexpected runtime exceptions."""

    error_type: str
    message: str


class PublicTrialRecord(BaseModel):
    """Pre-evaluation public run evidence safe to persist immediately."""

    trial_id: str
    suite_version: str = SUITE_VERSION
    scenario_id: str
    scenario_family: str
    scenario_title: str
    mode: RunMode
    official: bool
    model: str
    provider: str
    limits: CompositionLimits
    attempt_ceiling: int
    available_tool_ids: list[str] | None = None
    available_agent_template_ids: list[str] | None = None
    provider_preflight: dict[str, Any] | None = None
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str = ""
    status: AttemptStatus = "failed"
    public_scenario_hash: str
    public_scenario_ref: str
    injected_unavailable_template_id: str | None = None
    selected_plan: TeamCompositionPlan | None = None
    blocked_result: BlockedAttemptRecord | None = None
    failure_detail: FailureDetail | None = None
    validation_issue_history: list[list[PlanValidationIssue]] = Field(default_factory=list)
    recomposition_count: int = 0
    timings: dict[str, float | int | None] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    event_log: list[dict[str, Any]] = Field(default_factory=list)


class EvaluatedTrialRecord(PublicTrialRecord):
    """Post-evaluation record that may include private label references."""

    private_label_hash: str
    private_label_ref: str
    evaluation: CompositionEvaluation


class AggregateScenarioReport(BaseModel):
    """Aggregate report for one scenario across persisted attempts."""

    scenario_id: str
    family: str
    official: bool
    attempts: int
    successes: int
    blocked: int
    failed: int
    excluded: int
    strict_passes: int
    metrics: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AggregateReport(BaseModel):
    """Machine-readable suite aggregate with freeze metadata."""

    suite_version: str = SUITE_VERSION
    evaluator_version: str = EVALUATOR_VERSION
    mode: RunMode
    official: bool
    model: str
    provider: str
    limits: CompositionLimits
    attempt_ceiling: int
    public_suite_hash: str
    scenario_reports: list[AggregateScenarioReport] = Field(default_factory=list)
    retained_trial_files: list[str] = Field(default_factory=list)
    provider_preflight_blockers: list[dict[str, Any]] = Field(default_factory=list)


class OfficialFreeze(BaseModel):
    """Frozen accepted configuration for official Layer A collection."""

    suite_version: str = SUITE_VERSION
    evaluator_version: str = EVALUATOR_VERSION
    provider: str = "qwen"
    model: str = "qwen3.7-plus"
    limits: CompositionLimits = Field(default_factory=CompositionLimits)
    attempt_ceiling: int = 2


FROZEN_OFFICIAL_CONFIG = OfficialFreeze()


def relative_path(path: Path, root: Path) -> str:
    """Return a stable POSIX-style relative path for persisted evidence."""

    return path.resolve().relative_to(root.resolve()).as_posix()
