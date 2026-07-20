"""Qwendom society runner for the deterministic incident benchmark."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable

from agno.agent import Agent

from benchmarks import IncidentDecision, evaluate
from benchmarks.loader import build_prompt
from benchmarks.runtime import TrialResult, TrialUsage, hash_text, parse_decision
from benchmarks.single_agent import _extract_content, _serialize_content
from config import Settings

DEFAULT_SOCIETY_TIMEOUT_S = 900.0
DEFAULT_SYNTHESIS_TIMEOUT_S = 120.0
OrchestratorFactory = Callable[[Settings], Any]
SynthesisAgentFactory = Callable[[str, dict[str, Any]], Any]
ClockFn = Callable[[], datetime]

_TRACE_EVENTS = {
    "goal_discussed", "delegation_reported", "proposal_submitted",
    "proposal_challenged", "proposal_revised", "ballot_cast",
    "winner_selected", "leader_synthesis_completed", "task_complete",
}


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_orchestrator_factory(settings: Settings) -> Any:
    from society.orchestrator import SocietyOrchestrator
    return SocietyOrchestrator(settings=settings)


def _default_synthesis_factory(settings: Settings) -> SynthesisAgentFactory:
    from society.agents import build_model

    def factory(prompt: str, bundle: dict[str, Any]) -> Agent:
        return Agent(
            name="Benchmark Society Synthesizer",
            role="Integrate the society's evidence into the required decision schema.",
            model=build_model(settings),
            output_schema=IncidentDecision,
            structured_outputs=True,
            markdown=False,
            instructions=[
                "Use only the incident packet and society evidence bundle.",
                "Resolve conflicts using cited fact IDs; do not invent facts.",
                "Return only the IncidentDecision structured output.",
            ],
        )

    return factory


def build_evidence_bundle(orchestrator: Any, task_id: str) -> dict[str, Any]:
    """Extract task work products without exposing benchmark ground truth."""
    state = getattr(orchestrator, "session_states", {}).get(task_id, {})
    task = getattr(orchestrator, "tasks", {}).get(task_id)
    events = list(orchestrator.events.list(task_id))
    trace = [
        {"type": event.type, "actor": event.actor, "payload": event.payload}
        for event in events if event.type in _TRACE_EVENTS
    ]
    return {
        "final_answer": getattr(task, "final_answer", None),
        "working_brief": state.get("working_brief"),
        "subtasks": state.get("subtasks", []),
        "proposals": state.get("proposals", {}),
        "revisions": state.get("revisions", {}),
        "critique": state.get("critique"),
        "leader_synthesis": state.get("leader_synthesis"),
        "research_evidence": state.get("research_evidence", []),
        "final_deliverable": state.get("final_deliverable"),
        "governance_trace": trace,
    }


def _usage_from_summary(summary: dict[str, Any]) -> TrialUsage:
    return TrialUsage(
        model_calls=summary.get("total_calls"),
        input_tokens=summary.get("input_tokens"),
        output_tokens=summary.get("output_tokens"),
        total_tokens=summary.get("total_tokens"),
        cache_read_tokens=summary.get("cache_read_tokens"),
        cache_write_tokens=summary.get("cache_write_tokens"),
        reasoning_tokens=summary.get("reasoning_tokens"),
        cost=summary.get("cost"),
        duration=summary.get("duration"),
        usage_complete=bool(summary.get("usage_complete")),
    )


async def run_society(
    settings: Settings,
    *,
    orchestrator_factory: OrchestratorFactory | None = None,
    synthesis_agent_factory: SynthesisAgentFactory | None = None,
    clock: ClockFn | None = None,
    society_timeout_s: float = DEFAULT_SOCIETY_TIMEOUT_S,
    synthesis_timeout_s: float = DEFAULT_SYNTHESIS_TIMEOUT_S,
    max_total_tokens: int | None = None,
) -> TrialResult:
    """Run one society trial and preserve every terminal failure."""
    now = clock or _clock
    prompt = build_prompt(mode="society")
    result = TrialResult(
        mode="society", provider=settings.provider, model=settings.active_model,
        task_hash=hash_text("incident_decision_reference_task"),
        prompt_hash=hash_text(prompt),
    )
    if orchestrator_factory is None and (
        settings.provider != "qwen" or settings.qwen_model != "qwen3.7-plus"
    ):
        result.error = "benchmark requires qwen3.7-plus via provider='qwen'"
        return result

    started = now()
    result.started_at = started.isoformat()
    benchmark_settings = settings
    if orchestrator_factory is None:
        # The packet is self-contained, so both modes run without external research.
        benchmark_settings = settings.model_copy(update={"context7_mcp_enabled": False})
    orchestrator = (orchestrator_factory or _default_orchestrator_factory)(benchmark_settings)
    task = orchestrator.submit(prompt)
    result.task_id = task.id
    try:
        await asyncio.wait_for(orchestrator.run_task(task.id), timeout=society_timeout_s)
        task = orchestrator.tasks[task.id]
        if task.status != "complete":
            raise RuntimeError(f"society ended with status={task.status}")
        bundle = build_evidence_bundle(orchestrator, task.id)
        result.governance_trace = bundle["governance_trace"]
        synthesis_prompt = (
            f"{prompt}\n\n## Society evidence bundle\n"
            f"{json.dumps(bundle, sort_keys=True, default=str)}"
        )
        factory = synthesis_agent_factory or _default_synthesis_factory(benchmark_settings)
        agent = factory(synthesis_prompt, bundle)
        response = await asyncio.wait_for(agent.arun(synthesis_prompt), timeout=synthesis_timeout_s)
        if hasattr(orchestrator, "_capture_model_usage"):
            orchestrator._capture_model_usage(task.id, response, "benchmark_synthesis", "leader")
        content = _extract_content(response)
        result.raw_output = _serialize_content(content)
        decision = parse_decision(content)
        result.parsed_decision = decision
        result.evaluation = evaluate(decision, mode="society")
        result.usage = _usage_from_summary(orchestrator.model_usage_summary(task.id))
        if (
            max_total_tokens is not None
            and result.usage.total_tokens is not None
            and result.usage.total_tokens > max_total_tokens
        ):
            raise RuntimeError(
                f"token budget exceeded: {result.usage.total_tokens} > "
                f"{max_total_tokens}"
            )
        result.status = "success"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        if result.task_id and hasattr(orchestrator, "model_usage_summary"):
            result.usage = _usage_from_summary(orchestrator.model_usage_summary(result.task_id))
    finished = now()
    result.finished_at = finished.isoformat()
    result.wall_duration_s = max(0.0, (finished - started).total_seconds())
    return result
