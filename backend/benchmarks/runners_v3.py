"""Fair single-agent and Qwendom-society runners for benchmark suite v3."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable

from agno.agent import Agent

from benchmarks.evaluator_v3 import evaluate
from benchmarks.loader_v3 import build_prompt
from benchmarks.runtime import extract_metrics, hash_text
from benchmarks.runtime_v2 import ToolCallRecord
from benchmarks.runtime_v3 import SuiteV3TrialResult, elapsed_seconds, parse_answer
from benchmarks.single_agent import _extract_content, _extract_metrics_obj, _serialize_content
from benchmarks.society import _usage_from_summary, build_evidence_bundle
from benchmarks.suite_v2 import BenchmarkAnswer
from benchmarks.tools_v3 import (
    SHARED_TOOLS,
    inspect_auth_surface,
    inspect_release_pipeline_surface,
    inspect_supply_chain_surface,
    inspect_webhook_surface,
)
from config import Settings

ClockFn = Callable[[], datetime]
AgentFactory = Callable[[str], Any]
OrchestratorFactory = Callable[[Settings], Any]
SynthesisFactory = Callable[[str, dict[str, Any]], Any]
MAX_TOOL_CALLS = 16
REQUIRED_SURFACE_TOOLS = {
    "inspect_auth_surface",
    "inspect_webhook_surface",
    "inspect_supply_chain_surface",
    "inspect_release_pipeline_surface",
}
SURFACE_TOOLS = (
    inspect_auth_surface,
    inspect_webhook_surface,
    inspect_supply_chain_surface,
    inspect_release_pipeline_surface,
)


def _clock() -> datetime:
    """Return the current UTC time for production benchmark runs."""

    return datetime.now(timezone.utc)


def _validate_provider(settings: Settings) -> str | None:
    """Reject benchmark runs that do not use the frozen provider/model pair."""

    if settings.provider != "qwen" or settings.qwen_model != "qwen3.7-plus":
        return "benchmark v3 requires qwen3.7-plus via provider='qwen'"
    return None


def _default_agent(settings: Settings, name: str) -> Agent:
    """Create one schema-constrained Qwen agent with the shared v3 tool budget."""

    from society.agents import build_model

    return Agent(
        name=name,
        model=build_model(settings),
        output_schema=BenchmarkAnswer,
        structured_outputs=True,
        markdown=False,
        tools=SHARED_TOOLS,
        tool_call_limit=MAX_TOOL_CALLS,
        instructions=[
            "Use only the public task and shared read-only tools.",
            "The prompt contains results from all four required evidence surface tools.",
            "Citations must be arrays of exact S-record IDs, never prose or tool names.",
            "Return only the required BenchmarkAnswer JSON object.",
        ],
    )


def _tool_calls(response: Any) -> list[ToolCallRecord]:
    """Extract compact tool metadata without copying tool results into artifacts."""

    records: list[ToolCallRecord] = []
    for call in getattr(response, "tools", None) or []:
        name = getattr(call, "tool_name", None) or getattr(call, "name", None)
        arguments = getattr(call, "tool_args", None) or getattr(call, "arguments", None) or {}
        if isinstance(call, dict):
            name = call.get("tool_name") or call.get("name") or name
            arguments = call.get("tool_args") or call.get("arguments") or arguments
        if name:
            records.append(ToolCallRecord(
                name=str(name),
                arguments=arguments if isinstance(arguments, dict) else {},
            ))
    return records


def build_run_prompt() -> tuple[str, list[ToolCallRecord]]:
    """Prefetch the same public evidence surfaces for either benchmark mode."""

    evidence: dict[str, Any] = {}
    calls: list[ToolCallRecord] = []
    for tool in SURFACE_TOOLS:
        evidence[tool.__name__] = tool()
        calls.append(ToolCallRecord(name=tool.__name__, arguments={}))
    prompt = f"{build_prompt()}\n\nTOOL EVIDENCE:\n{json.dumps(evidence, sort_keys=True)}"
    return prompt, calls


def _base_result(settings: Settings, mode: str, prompt: str) -> SuiteV3TrialResult:
    """Create a result record whose prompt hash is identical across both modes."""

    return SuiteV3TrialResult(
        mode=mode,
        provider=settings.provider,
        model=settings.active_model,
        prompt_hash=hash_text(prompt),
    )


def _enforce_budgets(
    result: SuiteV3TrialResult,
    *,
    max_total_tokens: int | None,
) -> None:
    """Apply the same token and tool-call ceilings to either benchmark mode."""

    if len(result.tool_calls) > MAX_TOOL_CALLS:
        raise RuntimeError(f"tool-call budget exceeded: {len(result.tool_calls)} > {MAX_TOOL_CALLS}")
    called_tools = {call.name for call in result.tool_calls}
    missing_surfaces = sorted(REQUIRED_SURFACE_TOOLS - called_tools)
    if missing_surfaces:
        raise RuntimeError(
            "required evidence surfaces were not inspected: " + ", ".join(missing_surfaces)
        )
    if max_total_tokens is not None and (result.usage.total_tokens or 0) > max_total_tokens:
        raise RuntimeError(
            f"token budget exceeded: {result.usage.total_tokens} > {max_total_tokens}"
        )


async def run_single_task(
    settings: Settings,
    *,
    agent_factory: AgentFactory | None = None,
    clock: ClockFn | None = None,
    timeout_s: float = 300.0,
    max_total_tokens: int | None = None,
) -> SuiteV3TrialResult:
    """Run the v3 task through one Qwen agent and retain every failure."""

    now = clock or _clock
    prompt, prefetched_calls = build_run_prompt()
    result = _base_result(settings, "single_agent", prompt)
    result.tool_calls = prefetched_calls
    if agent_factory is None:
        if error := _validate_provider(settings):
            result.error = error
            return result
        agent_factory = lambda _prompt: _default_agent(settings, "Benchmark Single Agent")
    started = now()
    result.started_at = started.isoformat()
    try:
        response = await asyncio.wait_for(agent_factory(prompt).arun(prompt), timeout_s)
        content = _extract_content(response)
        result.raw_output = _serialize_content(content)
        result.tool_calls.extend(_tool_calls(response))
        result.usage = extract_metrics(_extract_metrics_obj(response))
        result.usage.model_calls = 1
        _enforce_budgets(result, max_total_tokens=max_total_tokens)
        result.parsed_answer = parse_answer(content)
        result.evaluation = evaluate(result.parsed_answer)
        result.status = "success"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finished = now()
    result.finished_at = finished.isoformat()
    result.wall_duration_s = elapsed_seconds(started, finished)
    return result


async def run_society_task(
    settings: Settings,
    *,
    orchestrator_factory: OrchestratorFactory | None = None,
    synthesis_factory: SynthesisFactory | None = None,
    clock: ClockFn | None = None,
    society_timeout_s: float = 780.0,
    synthesis_timeout_s: float = 180.0,
    max_total_tokens: int | None = None,
) -> SuiteV3TrialResult:
    """Run v3 through Qwendom and score a final schema-constrained synthesis."""

    now = clock or _clock
    prompt, prefetched_calls = build_run_prompt()
    result = _base_result(settings, "society", prompt)
    result.tool_calls = prefetched_calls
    if orchestrator_factory is None:
        if error := _validate_provider(settings):
            result.error = error
            return result
        from society.orchestrator import SocietyOrchestrator

        orchestrator_factory = lambda cfg: SocietyOrchestrator(settings=cfg)
    benchmark_settings = settings.model_copy(update={
        "context7_mcp_enabled": False,
        "benchmark_suite_tools_enabled": True,
        "benchmark_suite_version": "v3",
    })
    orchestrator = orchestrator_factory(benchmark_settings)
    started = now()
    result.started_at = started.isoformat()
    try:
        task = orchestrator.submit(prompt)
        result.task_runtime_id = task.id
        await asyncio.wait_for(orchestrator.run_task(task.id), society_timeout_s)
        task = orchestrator.tasks[task.id]
        result.society_terminal_status = task.status
        if task.status not in {"complete", "complete_with_warnings"}:
            raise RuntimeError(f"society ended with status={task.status}")

        bundle = build_evidence_bundle(orchestrator, task.id)
        result.governance_trace = bundle["governance_trace"]
        synthesis_prompt = (
            f"{prompt}\n\nSOCIETY EVIDENCE:\n"
            f"{json.dumps(bundle, sort_keys=True, default=str)}"
        )
        agent = synthesis_factory(synthesis_prompt, bundle) if synthesis_factory else _default_agent(
            benchmark_settings, "Benchmark Society Synthesizer"
        )
        response = await asyncio.wait_for(agent.arun(synthesis_prompt), synthesis_timeout_s)
        if hasattr(orchestrator, "_capture_model_usage"):
            orchestrator._capture_model_usage(task.id, response, "benchmark_v3_synthesis", "leader")
        content = _extract_content(response)
        result.raw_output = _serialize_content(content)
        result.tool_calls.extend(_tool_calls(response))
        for event in orchestrator.events.list(task.id):
            if event.type == "benchmark_tool_used":
                arguments = event.payload.get("arguments", {})
                result.tool_calls.append(ToolCallRecord(
                    name=str(event.payload.get("name", "unknown")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                ))
        result.usage = _usage_from_summary(orchestrator.model_usage_summary(task.id))
        _enforce_budgets(result, max_total_tokens=max_total_tokens)
        result.parsed_answer = parse_answer(content)
        result.evaluation = evaluate(result.parsed_answer)
        result.status = "success"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        if result.task_runtime_id and hasattr(orchestrator, "model_usage_summary"):
            result.usage = _usage_from_summary(orchestrator.model_usage_summary(result.task_runtime_id))
    finished = now()
    result.finished_at = finished.isoformat()
    result.wall_duration_s = elapsed_seconds(started, finished)
    return result
