"""Failure-preserving runtime records for benchmark v3."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from benchmarks.runtime import TrialUsage
from benchmarks.suite_v2 import BenchmarkAnswer
from benchmarks.runtime_v2 import ToolCallRecord


class SuiteV3TrialResult(BaseModel):
    trial_id: str = Field(default_factory=lambda: str(uuid4()))
    suite_version: str = "v3"
    task_id: str = "security-release-audit"
    task_kind: str = "security_release_audit"
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
    if isinstance(raw, BenchmarkAnswer):
        return raw
    if isinstance(raw, BaseModel):
        return BenchmarkAnswer.model_validate(raw.model_dump())
    if isinstance(raw, dict):
        return BenchmarkAnswer.model_validate(raw)
    if isinstance(raw, str):
        data = json.loads(raw.strip())
        return BenchmarkAnswer.model_validate(data)
    raise ValueError(f"unsupported answer type: {type(raw).__name__}")


def elapsed_seconds(started: datetime, finished: datetime) -> float:
    return max(0.0, (finished - started).total_seconds())
