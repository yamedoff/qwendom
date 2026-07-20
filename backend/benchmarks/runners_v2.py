"""Single-agent and Qwendom-society runners for benchmark suite v2."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable

from agno.agent import Agent

from benchmarks.evaluator_v2 import evaluate_task
from benchmarks.loader_v2 import build_task_prompt
from benchmarks.runtime import TrialUsage, extract_metrics, hash_text
from benchmarks.runtime_v2 import SuiteTrialResult, ToolCallRecord, elapsed_seconds, parse_answer
from benchmarks.single_agent import _extract_content, _extract_metrics_obj, _serialize_content
from benchmarks.society import build_evidence_bundle, _usage_from_summary
from benchmarks.suite_v2 import BenchmarkAnswer, TASKS
from benchmarks.tools_v2 import calculate, lookup_dataset, lookup_record
from config import Settings

ClockFn = Callable[[], datetime]
AgentFactory = Callable[[str], Any]
OrchestratorFactory = Callable[[Settings], Any]
SynthesisFactory = Callable[[str, dict[str, Any]], Any]
SHARED_TOOLS = [lookup_record, lookup_dataset, calculate]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _validate_provider(settings: Settings) -> str | None:
    if settings.provider != "qwen" or settings.qwen_model != "qwen3.7-plus":
        return "benchmark v2 requires qwen3.7-plus via provider='qwen'"
    return None


def _default_agent(settings: Settings, name: str) -> Agent:
    from society.agents import build_model
    return Agent(
        name=name, model=build_model(settings), output_schema=BenchmarkAnswer,
        structured_outputs=True, markdown=False, tools=SHARED_TOOLS,
        tool_call_limit=8,
        instructions=[
            "Use only the public task and shared read-only tools.",
            "Return only the required BenchmarkAnswer JSON object.",
        ],
    )


def _tool_calls(response: Any) -> list[ToolCallRecord]:
    """Extract tool name and arguments while excluding potentially large results."""
    records: list[ToolCallRecord] = []
    for call in getattr(response, "tools", None) or []:
        name = getattr(call, "tool_name", None) or getattr(call, "name", None)
        args = getattr(call, "tool_args", None) or getattr(call, "arguments", None) or {}
        if isinstance(call, dict):
            name = call.get("tool_name") or call.get("name") or name
            args = call.get("tool_args") or call.get("arguments") or args
        if name:
            records.append(ToolCallRecord(name=str(name), arguments=args if isinstance(args, dict) else {}))
    return records


def _base_result(settings: Settings, task_id: str, mode: str) -> SuiteTrialResult:
    task = TASKS[task_id]
    prompt = build_task_prompt(task_id)
    return SuiteTrialResult(
        task_id=task_id, task_kind=task.kind, mode=mode,
        provider=settings.provider, model=settings.active_model,
        prompt_hash=hash_text(prompt),
    )


async def run_single_task(
    settings: Settings, task_id: str, *, agent_factory: AgentFactory | None = None,
    clock: ClockFn | None = None, timeout_s: float = 120.0,
    max_total_tokens: int | None = None,
) -> SuiteTrialResult:
    """Run one v2 task through one Qwen agent, preserving all failures."""
    now, prompt = clock or _clock, build_task_prompt(task_id)
    result = _base_result(settings, task_id, "single_agent")
    if agent_factory is None:
        if error := _validate_provider(settings):
            result.error = error
            return result
        agent_factory = lambda _prompt: _default_agent(settings, "Benchmark Single Agent")
    started = now(); result.started_at = started.isoformat()
    try:
        response = await asyncio.wait_for(agent_factory(prompt).arun(prompt), timeout_s)
        content = _extract_content(response)
        result.raw_output = _serialize_content(content)
        result.tool_calls = _tool_calls(response)
        result.usage = extract_metrics(_extract_metrics_obj(response)); result.usage.model_calls = 1
        if max_total_tokens is not None and (result.usage.total_tokens or 0) > max_total_tokens:
            raise RuntimeError(f"token budget exceeded: {result.usage.total_tokens} > {max_total_tokens}")
        result.parsed_answer = parse_answer(content)
        result.evaluation = evaluate_task(task_id, result.parsed_answer)
        result.status = "success"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finished = now(); result.finished_at = finished.isoformat()
    result.wall_duration_s = elapsed_seconds(started, finished)
    return result


async def run_society_task(
    settings: Settings, task_id: str, *,
    orchestrator_factory: OrchestratorFactory | None = None,
    synthesis_factory: SynthesisFactory | None = None,
    clock: ClockFn | None = None, society_timeout_s: float = 780.0,
    synthesis_timeout_s: float = 120.0, max_total_tokens: int | None = None,
) -> SuiteTrialResult:
    """Run one v2 task through Qwendom, then deterministically score synthesis."""
    now, prompt = clock or _clock, build_task_prompt(task_id)
    result = _base_result(settings, task_id, "society")
    if orchestrator_factory is None:
        if error := _validate_provider(settings):
            result.error = error
            return result
        from society.orchestrator import SocietyOrchestrator
        orchestrator_factory = lambda cfg: SocietyOrchestrator(settings=cfg)
    benchmark_settings = settings.model_copy(update={
        "context7_mcp_enabled": False,
        "benchmark_suite_tools_enabled": True,
    })
    orchestrator = orchestrator_factory(benchmark_settings)
    started = now(); result.started_at = started.isoformat()
    try:
        task = orchestrator.submit(prompt); result.task_runtime_id = task.id
        await asyncio.wait_for(orchestrator.run_task(task.id), society_timeout_s)
        task = orchestrator.tasks[task.id]
        result.society_terminal_status = task.status
        if task.status not in {"complete", "complete_with_warnings"}:
            raise RuntimeError(f"society ended with status={task.status}")
        bundle = build_evidence_bundle(orchestrator, task.id)
        result.governance_trace = bundle["governance_trace"]
        synthesis_prompt = f"{prompt}\n\nSOCIETY EVIDENCE:\n{json.dumps(bundle, sort_keys=True, default=str)}"
        agent = synthesis_factory(synthesis_prompt, bundle) if synthesis_factory else _default_agent(
            benchmark_settings, "Benchmark Society Synthesizer"
        )
        response = await asyncio.wait_for(agent.arun(synthesis_prompt), synthesis_timeout_s)
        if hasattr(orchestrator, "_capture_model_usage"):
            orchestrator._capture_model_usage(task.id, response, "benchmark_v2_synthesis", "leader")
        content = _extract_content(response)
        result.raw_output = _serialize_content(content); result.tool_calls = _tool_calls(response)
        for event in orchestrator.events.list(task.id):
            if event.type == "benchmark_tool_used":
                arguments = event.payload.get("arguments", {})
                result.tool_calls.append(ToolCallRecord(
                    name=str(event.payload.get("name", "unknown")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                ))
        result.parsed_answer = parse_answer(content)
        result.evaluation = evaluate_task(task_id, result.parsed_answer)
        result.usage = _usage_from_summary(orchestrator.model_usage_summary(task.id))
        if max_total_tokens is not None and (result.usage.total_tokens or 0) > max_total_tokens:
            raise RuntimeError(f"token budget exceeded: {result.usage.total_tokens} > {max_total_tokens}")
        result.status = "success"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        if result.task_runtime_id and hasattr(orchestrator, "model_usage_summary"):
            result.usage = _usage_from_summary(orchestrator.model_usage_summary(result.task_runtime_id))
    finished = now(); result.finished_at = finished.isoformat()
    result.wall_duration_s = elapsed_seconds(started, finished)
    return result
