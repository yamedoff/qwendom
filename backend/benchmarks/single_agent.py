"""Slice 2A — single-agent benchmark runner for the IncidentDecision task.

Runs a qwen3.7-plus agent against the reference incident packet, captures
usage and timing, parses the structured output, and invokes the pure
evaluator.  All external dependencies (agent construction, wall clock) are
injectable so that tests never touch a real provider.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable

from agno.agent import Agent

from benchmarks import IncidentDecision, evaluate
from benchmarks.loader import build_prompt
from benchmarks.runtime import (
    TrialResult,
    extract_metrics,
    hash_text,
    parse_decision,
)
from config import Settings

BENCHMARK_INSTRUCTIONS: list[str] = [
    "You are analysing a production incident.  Reason only from the fact IDs provided in the packet.",
    "Do not use tools or external evidence — the packet is self-contained.",
    "Select exactly one root-cause hypothesis, recommend ordered actions, recommend controls, and cite evidence using fact IDs only.",
    "Return ONLY the IncidentDecision JSON schema.  Do not include prose, markdown, or commentary.",
]

DEFAULT_TIMEOUT_S: float = 120.0

AgentFactory = Callable[[str], Agent]
ClockFn = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _build_default_agent_factory(settings: Settings) -> AgentFactory:
    from society.agents import build_model

    def factory(prompt: str) -> Agent:
        model = build_model(settings)
        return Agent(
            model=model,
            output_schema=IncidentDecision,
            structured_outputs=True,
            markdown=False,
            instructions=BENCHMARK_INSTRUCTIONS,
        )

    return factory


async def run_single_agent(
    settings: Settings,
    *,
    agent_factory: AgentFactory | None = None,
    clock: ClockFn | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_total_tokens: int | None = None,
) -> TrialResult:
    """Execute a single benchmark trial.

    Args:
        settings: Runtime configuration.  Must have ``provider == 'qwen'``
            and ``qwen_model == 'qwen3.7-plus'`` unless *agent_factory* is
            provided (test-only bypass).
        agent_factory: Injectable callable that receives the prompt and
            returns a ready-to-run :class:`Agent`.  When ``None`` the
            default factory builds a real qwen agent from *settings*.
        clock: Injectable clock returning a ``datetime`` for timestamps.
        timeout_s: Hard wall-clock timeout in seconds.

    Returns:
        A :class:`TrialResult` capturing every aspect of the trial.
    """
    _clock = clock or _default_clock
    prompt = build_prompt(mode="single_agent")
    task_hash = hash_text("incident_decision_reference_task")
    prompt_hash = hash_text(prompt)

    result = TrialResult(
        mode="single_agent",
        provider=settings.provider,
        model=settings.active_model,
        task_hash=task_hash,
        prompt_hash=prompt_hash,
    )

    if agent_factory is None:
        if settings.provider != "qwen":
            result.status = "failed"
            result.error = (
                f"benchmark requires provider='qwen', got '{settings.provider}'"
            )
            return result
        if settings.qwen_model != "qwen3.7-plus":
            result.status = "failed"
            result.error = (
                f"benchmark requires qwen_model='qwen3.7-plus', got '{settings.qwen_model}'"
            )
            return result
        agent_factory = _build_default_agent_factory(settings)

    started = _clock()
    result.started_at = started.isoformat()

    try:
        agent = agent_factory(prompt)
        run_response = await asyncio.wait_for(
            agent.arun(prompt),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        finished = _clock()
        result.finished_at = finished.isoformat()
        result.wall_duration_s = (finished - started).total_seconds()
        result.status = "failed"
        result.error = f"timeout after {timeout_s}s: {exc}"
        return result
    except Exception as exc:
        finished = _clock()
        result.finished_at = finished.isoformat()
        result.wall_duration_s = (finished - started).total_seconds()
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    finished = _clock()
    result.finished_at = finished.isoformat()
    result.wall_duration_s = (finished - started).total_seconds()

    content = _extract_content(run_response)
    result.raw_output = _serialize_content(content)

    metrics_obj = _extract_metrics_obj(run_response)
    result.usage = extract_metrics(metrics_obj)
    result.usage.model_calls = 1
    if (
        max_total_tokens is not None
        and result.usage.total_tokens is not None
        and result.usage.total_tokens > max_total_tokens
    ):
        result.status = "failed"
        result.error = (
            f"token budget exceeded: {result.usage.total_tokens} > "
            f"{max_total_tokens}"
        )
        return result

    try:
        decision = parse_decision(content)
    except Exception as exc:
        result.status = "failed"
        result.error = f"parse failure: {exc}"
        return result

    result.parsed_decision = decision
    result.evaluation = evaluate(decision, mode="single_agent")
    result.status = "success"
    return result


def _extract_content(response: Any) -> Any:
    if hasattr(response, "content"):
        content = response.content
        if content is not None:
            return content
    if hasattr(response, "structured_output"):
        so = response.structured_output
        if so is not None:
            return so
    return response


def _serialize_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, sort_keys=True, default=str)
    if hasattr(content, "model_dump"):
        return json.dumps(content.model_dump(), sort_keys=True, default=str)
    return str(content)


def _extract_metrics_obj(response: Any) -> Any:
    if hasattr(response, "metrics"):
        return response.metrics
    if hasattr(response, "usage"):
        return response.usage
    return None
