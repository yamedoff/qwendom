"""Runtime artefacts for individual tasks in benchmark suite v2."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from benchmarks.runtime import TrialUsage
from benchmarks.suite_v2 import BenchmarkAnswer, TaskKind


class ToolCallRecord(BaseModel):
    """Auditable record of a model-requested benchmark tool call."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class SuiteTrialResult(BaseModel):
    """Failure-preserving result for one mode/task/attempt tuple."""

    trial_id: str = Field(default_factory=lambda: str(uuid4()))
    suite_version: str = "v2"
    task_id: str
    task_kind: TaskKind
    mode: Literal["single_agent", "society"]
    provider: str
    model: str
    prompt_hash: str
    started_at: str = ""
    finished_at: str = ""
    wall_duration_s: float = 0.0
    status: Literal["success", "failed"] = "failed"
    raw_output: str | None = None
    parsed_answer: BenchmarkAnswer | None = None
    evaluation: dict[str, Any] | None = None
    usage: TrialUsage = Field(default_factory=TrialUsage)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    task_runtime_id: str | None = None
    society_terminal_status: str | None = None
    governance_trace: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


def parse_answer(raw: Any) -> BenchmarkAnswer:
    """Parse a strict answer object without mining JSON from prose."""
    if isinstance(raw, BenchmarkAnswer):
        return raw
    if isinstance(raw, BaseModel):
        return BenchmarkAnswer.model_validate(raw.model_dump())
    if isinstance(raw, dict):
        return BenchmarkAnswer.model_validate(raw)
    if isinstance(raw, str):
        data = json.loads(raw.strip())
        if not isinstance(data, dict):
            raise ValueError("benchmark answer must be a JSON object")
        return BenchmarkAnswer.model_validate(data)
    raise ValueError(f"unsupported answer type: {type(raw).__name__}")


def elapsed_seconds(started: datetime, finished: datetime) -> float:
    """Return a non-negative elapsed duration for injected clocks."""
    return max(0.0, (finished - started).total_seconds())
