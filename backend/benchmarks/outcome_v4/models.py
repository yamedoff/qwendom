"""Typed models shared by the deterministic Layer B harness."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RunMode = Literal["development", "official"]
BenchmarkMode = Literal["single_agent", "society"]
ScenarioId = Literal["incident_repair", "screenshot_to_product"]
AttemptStatus = Literal["success", "failed", "refused"]

SUITE_VERSION = "outcome-v4-layer-b-dev-foundation-2026-07-15"
EVALUATOR_VERSION = "1.0.0"
REQUIRED_MODEL = "qwen3.7-plus"
BENCHMARK_MODELS: tuple[str, ...] = (REQUIRED_MODEL,)


def canonical_json(value: Any) -> str:
    """Return deterministic JSON suitable for hashing and persistence."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True)


def stable_hash(value: Any) -> str:
    """Return a deterministic SHA-256 digest for *value*."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    """Return the current UTC time in a stable ISO-8601 format."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def relative_to(path: Path, root: Path) -> str:
    """Return a stable POSIX-style relative path."""

    return path.resolve().relative_to(root.resolve()).as_posix()


class RetryPolicy(BaseModel):
    """Frozen retry policy shared by both modes."""

    max_attempts: int = Field(default=2, ge=1)
    backoff_base_seconds: float = Field(default=0.25, ge=0.0)
    backoff_cap_seconds: float = Field(default=1.0, ge=0.0)


class BudgetCaps(BaseModel):
    """Mode-neutral atomic budget ceilings persisted with every attempt."""

    qwen_input_tokens: int = Field(default=600_000, ge=0)
    qwen_output_tokens: int = Field(default=600_000, ge=0)
    qwen_total_tokens: int = Field(default=600_000, ge=0)
    qwen_model_calls: int = Field(default=24, ge=0)
    image_credits: int = Field(default=6, ge=0)
    vision_calls: int = Field(default=12, ge=0)
    browser_renders: int = Field(default=20, ge=0)
    subprocess_seconds: float = Field(default=20.0, ge=0.0)
    retries_failed_calls: int = Field(default=6, ge=0)
    wall_time_seconds: float = Field(default=300.0, ge=0.0)
    peak_concurrency: int = Field(default=4, ge=1)


class BudgetSnapshot(BaseModel):
    """Current budget consumption snapshot."""

    qwen_input_tokens: int = 0
    qwen_output_tokens: int = 0
    qwen_total_tokens: int = 0
    qwen_model_calls: int = 0
    image_credits: int = 0
    vision_calls: int = 0
    browser_renders: int = 0
    subprocess_seconds: float = 0.0
    retries_failed_calls: int = 0
    wall_time_seconds: float = 0.0
    peak_concurrency: int = 0
    current_concurrency: int = 0


class OutputContract(BaseModel):
    """Public output contract shared by both execution modes."""

    required_paths: list[str] = Field(default_factory=list)
    required_reports: list[str] = Field(default_factory=list)
    required_cleanup_markers: list[str] = Field(default_factory=list)


class PublicScenarioSurface(BaseModel):
    """Public scenario data visible to both modes and provider adapters."""

    scenario_id: ScenarioId
    title: str
    summary: str
    brief_markdown: str
    fixture_source_dir: str
    tool_union: list[str] = Field(default_factory=list)
    output_contract: OutputContract

    @property
    def public_hash(self) -> str:
        """Return the stable hash for the public scenario surface."""

        return stable_hash(self)


class FutureVariantSlot(BaseModel):
    """Placeholder for one future official variant slot."""

    variant_id: str
    description: str
    sealed: bool = False
    fixture_hash: str | None = None
    evaluator_hash: str | None = None


class SealManifest(BaseModel):
    """Unsealed future official variant manifest."""

    scenario_id: ScenarioId
    status: Literal["unsealed", "sealed"] = "unsealed"
    variants: list[FutureVariantSlot] = Field(default_factory=list)


class PromotionGateReport(BaseModel):
    """Official-run gate evaluation."""

    ready: bool
    reasons: list[str] = Field(default_factory=list)


class EvaluatorCheck(BaseModel):
    """One deterministic evaluator check."""

    name: str
    passed: bool
    points: float
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluatorResult(BaseModel):
    """Structured scenario score plus mandatory-gate truth."""

    evaluator_version: str = EVALUATOR_VERSION
    scenario_id: ScenarioId
    passed: bool
    total_score: float
    mandatory_gates_passed: bool
    checks: list[EvaluatorCheck] = Field(default_factory=list)
    aesthetic_score: float | None = None


class ToolTraceRecord(BaseModel):
    """Durable trace of one tool-like operation."""

    tool_id: str
    category: str
    amount: float
    started_at: float
    finished_at: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttemptRecord(BaseModel):
    """Durable attempt bundle summary."""

    attempt_id: str
    suite_version: str = SUITE_VERSION
    run_mode: RunMode
    benchmark_mode: BenchmarkMode
    scenario_id: ScenarioId
    model: str = REQUIRED_MODEL
    tool_union: list[str] = Field(default_factory=list)
    public_scenario_hash: str
    fixture_source_hash: str
    fixture_copy_hash: str = ""
    fixture_copy_dir: str = ""
    prompt_text: str
    prompt_hash: str
    timeout_seconds: float
    retry_policy: RetryPolicy
    budgets: BudgetCaps
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str = ""
    status: AttemptStatus = "failed"
    refusal_reasons: list[str] = Field(default_factory=list)
    raw_outputs: dict[str, Any] = Field(default_factory=dict)
    failure: dict[str, str] | None = None
    provider_adapter: str = ""
    usage: BudgetSnapshot = Field(default_factory=BudgetSnapshot)
    cleanup_evidence: list[str] = Field(default_factory=list)
    hashes: dict[str, str] = Field(default_factory=dict)
    graph_result: dict[str, Any] | None = None
    evaluator_result: EvaluatorResult | None = None
