"""Slice 2A runtime models and safe parsing helpers.

Provides the data contracts that flow between a single-agent trial runner
and the deterministic evaluator.  No production files are imported or
modified — this module is self-contained within the benchmarks package.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator, computed_field

from benchmarks import EvaluationResult, IncidentDecision


class TrialUsage(BaseModel):
    """Token-level usage metrics captured from a single trial run.

    Field names follow the Agno ``RunMetrics`` convention.
    """

    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost: float | None = None
    duration: float | None = None
    time_to_first_token: float | None = None
    usage_complete: bool = Field(
        default=False,
        description="True only when input_tokens, output_tokens, and total_tokens are all present.",
    )

    @model_validator(mode="after")
    def _compute_usage_complete(self) -> "TrialUsage":
        self.usage_complete = (
            isinstance(self.input_tokens, int)
            and isinstance(self.output_tokens, int)
            and isinstance(self.total_tokens, int)
        )
        return self


class TrialResult(BaseModel):
    """Complete artefact produced by a single benchmark trial."""

    trial_id: str = Field(default_factory=lambda: str(uuid4()))
    mode: Literal["single_agent", "society"] = "single_agent"
    provider: str = ""
    model: str = ""
    task_hash: str = ""
    prompt_hash: str = ""
    started_at: str = ""
    finished_at: str = ""
    wall_duration_s: float = 0.0
    status: Literal["success", "failed"] = "failed"
    raw_output: str | None = None
    parsed_decision: IncidentDecision | None = None
    evaluation: EvaluationResult | None = None
    usage: TrialUsage = Field(default_factory=TrialUsage)
    error: str | None = None
    task_id: str | None = None
    governance_trace: list[dict[str, Any]] = Field(default_factory=list)


def hash_text(text: str) -> str:
    """Return the SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_decision(raw: Any) -> IncidentDecision:
    """Parse *raw* into an :class:`IncidentDecision`.

    Accepted inputs (in order of preference):

    1. An existing :class:`IncidentDecision` instance — returned as-is.
    2. Any other Pydantic ``BaseModel`` — converted via ``model_dump()``.
    3. A ``dict`` — validated directly.
    4. A ``str`` containing a **strict full JSON object** — parsed then
       validated.  Embedded-prose mining is **not** performed; the string
       must be a complete JSON document.

    Raises:
        ValueError: If *raw* cannot be interpreted as an IncidentDecision.
    """
    if isinstance(raw, IncidentDecision):
        return raw

    if isinstance(raw, BaseModel):
        return IncidentDecision.model_validate(raw.model_dump())

    if isinstance(raw, dict):
        return IncidentDecision.model_validate(raw)

    if isinstance(raw, str):
        stripped = raw.strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw output is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"expected a JSON object at top level, got {type(data).__name__}"
            )
        return IncidentDecision.model_validate(data)

    raise ValueError(
        f"cannot parse decision from {type(raw).__name__}; "
        "expected IncidentDecision, BaseModel, dict, or strict JSON string"
    )


def extract_metrics(metrics_obj: Any) -> TrialUsage:
    """Extract factual usage metrics from a ``RunMetrics`` object.

    Calls ``to_dict()`` on the object and maps Agno field names.
    ``usage_complete`` is ``True`` only when ``input_tokens``,
    ``output_tokens``, and ``total_tokens`` are all present as ints.
    Optional fields (``cache_read_tokens``, ``cache_write_tokens``,
    ``reasoning_tokens``, ``cost``, ``duration``, ``time_to_first_token``)
    may be ``None``.
    """
    if metrics_obj is None:
        return TrialUsage()

    try:
        data: dict[str, Any] = metrics_obj.to_dict()
    except Exception:
        return TrialUsage()

    if not isinstance(data, dict):
        return TrialUsage()

    input_t = data.get("input_tokens")
    output_t = data.get("output_tokens")
    total_t = data.get("total_tokens")

    complete = (
        isinstance(input_t, int)
        and isinstance(output_t, int)
        and isinstance(total_t, int)
    )

    def _opt_int(key: str) -> int | None:
        v = data.get(key)
        return v if isinstance(v, int) else None

    def _opt_float(key: str) -> float | None:
        v = data.get(key)
        return v if isinstance(v, (int, float)) else None

    return TrialUsage(
        input_tokens=_opt_int("input_tokens"),
        output_tokens=_opt_int("output_tokens"),
        total_tokens=_opt_int("total_tokens"),
        cache_read_tokens=_opt_int("cache_read_tokens"),
        cache_write_tokens=_opt_int("cache_write_tokens"),
        reasoning_tokens=_opt_int("reasoning_tokens"),
        cost=_opt_float("cost"),
        duration=_opt_float("duration"),
        time_to_first_token=_opt_float("time_to_first_token"),
        usage_complete=complete,
    )


def utc_now() -> datetime:
    """Return the current UTC time (injectable via clock in tests)."""
    return datetime.now(timezone.utc)
