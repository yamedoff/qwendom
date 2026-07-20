"""Dependency-injected development adapters for the Layer B harness."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agno.agent import Agent
from config import Settings
from pydantic import BaseModel

from society.agents import build_agno_agent, build_model
from society.composition_runtime import CompositionRuntime
from society.models import SocietyAgent
from society.provider_preflight import model_capability_preflight
from society.schemas.team_composition import CompositionLimits, TeamAssignment, TeamCompositionPlan, ToolGrant, WorkNode
from society.specialist_selection import (
    AgnoFixedSpecialistSelectionProvider,
    FixedSpecialistCoordinator,
    FixedSpecialistProviderError,
    InvokeSpecialistCall,
    SpecialistSelectionError,
)
from society.team_composer import TeamComposer
from society.tools.agentbay import AgentBayTools
from society.tools.local_artifacts import LocalArtifactTools
from society.work_graph import BoundedWorkGraphExecutor, WorkGraphTerminalStatus

from .artifacts import atomic_write_json, atomic_write_text
from .budget import AtomicBudgetLedger
from .models import BenchmarkMode, PublicScenarioSurface, RetryPolicy, ToolTraceRecord


class AdapterRunResult(Protocol):
    raw_outputs: dict[str, Any]
    cleanup_evidence: list[str]
    tool_traces: list[ToolTraceRecord]
    graph_result: dict[str, Any] | None


class ModeAdapter(Protocol):
    """Injected adapter seam for one benchmark mode."""

    deterministic: bool
    name: str

    async def run(
        self,
        *,
        scenario: PublicScenarioSurface,
        workspace_dir: Path,
        prompt_text: str,
        ledger: AtomicBudgetLedger,
        retry_policy: RetryPolicy,
    ) -> AdapterRunResult:
        """Run one scenario and return raw attempt evidence."""


class DeterministicToolSurface:
    """Small deterministic tool surface shared by fake adapters."""

    def __init__(self, workspace_dir: Path, ledger: AtomicBudgetLedger) -> None:
        self.workspace_dir = workspace_dir
        self.ledger = ledger
        self.traces: list[ToolTraceRecord] = []

    async def read_text(self, relative_path: str) -> str:
        return await self._run("read_fixture", "vision_calls", 1.0, relative_path, lambda: (self.workspace_dir / relative_path).read_text(encoding="utf-8"))

    async def write_text(self, relative_path: str, content: str) -> None:
        await self._run("filesystem_edit", "qwen_model_calls", 1.0, relative_path, lambda: atomic_write_text(self.workspace_dir / relative_path, content))

    async def write_json(self, relative_path: str, payload: dict[str, Any]) -> None:
        await self._run("write_report", "qwen_model_calls", 1.0, relative_path, lambda: atomic_write_json(self.workspace_dir / relative_path, payload))

    async def copy_file(self, source_relative_path: str, destination_relative_path: str) -> None:
        def _copy() -> None:
            source = self.workspace_dir / source_relative_path
            destination = self.workspace_dir / destination_relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

        await self._run("copy_asset", "browser_renders", 1.0, destination_relative_path, _copy)

    async def record_subprocess_seconds(self, seconds: float, label: str) -> None:
        await self._run("subprocess_check", "subprocess_seconds", seconds, label, lambda: None)

    async def _run(
        self,
        tool_id: str,
        category: str,
        amount: float,
        path_label: str,
        action: Callable[[], Any],
    ) -> Any:
        async with self.ledger.tool_call(tool_id, category=category, amount=amount, metadata={"path": path_label}) as trace:
            result = action()
            await asyncio.sleep(0.01)
            trace.finished_at = time.perf_counter()
            self.traces.append(trace)
            return result


class FakeAdapterResult:
    """Concrete adapter result used by deterministic fake runs."""

    def __init__(
        self,
        *,
        raw_outputs: dict[str, Any],
        cleanup_evidence: list[str],
        tool_traces: list[ToolTraceRecord],
        graph_result: dict[str, Any] | None = None,
    ) -> None:
        self.raw_outputs = raw_outputs
        self.cleanup_evidence = cleanup_evidence
        self.tool_traces = tool_traces
        self.graph_result = graph_result


class WorkspaceExportViolation(RuntimeError):
    """Typed durable export containment failure."""

    def __init__(self, *, code: str, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": "workspace_export_violation",
            "reason": str(self),
            **self.detail,
        }


class DeterministicSingleAgentAdapter:
    """Mode-neutral fake single-agent runner for development-only tests."""

    deterministic = True
    name = "deterministic-single-agent"

    async def run(
        self,
        *,
        scenario: PublicScenarioSurface,
        workspace_dir: Path,
        prompt_text: str,
        ledger: AtomicBudgetLedger,
        retry_policy: RetryPolicy,
    ) -> FakeAdapterResult:
        tools = DeterministicToolSurface(workspace_dir, ledger)
        await ledger.debit("qwen_input_tokens", float(min(2000, len(prompt_text))))
        await ledger.debit("qwen_output_tokens", 900.0)
        if scenario.scenario_id == "incident_repair":
            await _materialize_incident_solution(tools)
        else:
            await _materialize_product_solution(tools)
        return FakeAdapterResult(
            raw_outputs={
                "mode": "single_agent",
                "summary": f"completed {scenario.scenario_id} with deterministic fake tools",
            },
            cleanup_evidence=["cleanup/cleanup.json"],
            tool_traces=tools.traces,
        )


class DeterministicSocietyAdapter:
    """Development-only fake society runner built on the production work graph."""

    deterministic = True
    name = "deterministic-society"

    async def run(
        self,
        *,
        scenario: PublicScenarioSurface,
        workspace_dir: Path,
        prompt_text: str,
        ledger: AtomicBudgetLedger,
        retry_policy: RetryPolicy,
    ) -> FakeAdapterResult:
        await ledger.debit("qwen_input_tokens", float(min(2000, len(prompt_text))))
        await ledger.debit("qwen_output_tokens", 900.0)
        tools = DeterministicToolSurface(workspace_dir, ledger)
        plan = _build_society_plan(scenario.scenario_id)
        events: list[dict[str, Any]] = []

        async def event_sink(event_type: str, payload: dict[str, Any]) -> None:
            events.append({"event_type": event_type, "payload": dict(payload)})

        async def node_runner(node: WorkNode, assignment: TeamAssignment, attempt: int, cancellation_event: asyncio.Event) -> dict[str, Any]:
            if scenario.scenario_id == "incident_repair":
                return await _incident_node_runner(node.id, tools)
            return await _product_node_runner(node.id, tools)

        executor = BoundedWorkGraphExecutor(
            plan,
            CompositionLimits(max_model_workers=4, max_agentbay_sessions=0, max_media_jobs=0),
            node_runner,
            event_sink,
        )
        graph_result = await executor.run()
        return FakeAdapterResult(
            raw_outputs={
                "mode": "society",
                "summary": f"completed {scenario.scenario_id} with deterministic fake society",
                "events": events,
            },
            cleanup_evidence=["cleanup/cleanup.json"],
            tool_traces=tools.traces,
            graph_result=graph_result.model_dump(mode="json"),
        )


class ProviderBackedSingleAgentAdapter:
    """Development smoke adapter that runs one real Qwen agent with Agno tools."""

    deterministic = False
    name = "provider-single-agent"

    def __init__(
        self,
        *,
        settings_factory: Callable[[RetryPolicy], Settings] | None = None,
        preflight_fn: Callable[[Settings], dict[str, Any]] = model_capability_preflight,
        build_agent_factory: Callable[..., Any] = build_agno_agent,
        agentbay_toolkit_factory: Callable[..., Any] = AgentBayTools,
    ) -> None:
        self._settings_factory = settings_factory or _default_provider_settings
        self._preflight_fn = preflight_fn
        self._build_agent_factory = build_agent_factory
        self._agentbay_toolkit_factory = agentbay_toolkit_factory

    async def run(
        self,
        *,
        scenario: PublicScenarioSurface,
        workspace_dir: Path,
        prompt_text: str,
        ledger: AtomicBudgetLedger,
        retry_policy: RetryPolicy,
    ) -> FakeAdapterResult:
        settings = self._settings_factory(retry_policy)
        typed_blockers = _provider_blockers_for_scenario(
            scenario,
            preflight=self._preflight_fn(settings),
            available_tool_ids=_single_agent_tool_ids(scenario.scenario_id),
        )
        if typed_blockers:
            raise ProviderAdapterRefusal.from_blockers(typed_blockers)

        task_id = f"outcome-v4-single-{scenario.scenario_id}"
        events: list[dict[str, Any]] = []
        tool_traces: list[ToolTraceRecord] = []
        model_usage: list[dict[str, Any]] = []
        cleanup_steps: list[str] = []
        toolkit = self._agentbay_toolkit_factory(
            task_id=task_id,
            role_key="builder",
            allowed_remote_roots=["/workspace"],
            artifact_root=workspace_dir / "_provider_artifacts" / "single_agent",
            settings=settings,
            event_sink=lambda event: events.append(dict(event)),
            extra_allowed_tool_ids=["browser_render"] if scenario.scenario_id == "screenshot_to_product" else [],
            workspace_source_dir=workspace_dir,
        )
        wrapped_tools = _single_agent_tools(toolkit, task_id, ledger, tool_traces, scenario.scenario_id)
        fairness = _single_agent_fairness_proof(
            scenario.scenario_id,
            [getattr(tool, "__name__", type(tool).__name__) for tool in wrapped_tools],
        )
        _assert_fairness_alignment(single_agent_proof=fairness)
        identity = SocietyAgent(
            id="outcome-v4-single-agent",
            name="Outcome V4 Single Agent",
            role="builder",
            skills=["implementation", "sandbox_execution", "artifact_export", "artifact_inspection"],
        )
        agent = _BudgetedAgentProxy(
            self._build_agent_factory(
                identity,
                settings,
                tools=wrapped_tools,
                session_id=f"{task_id}-session",
                session_state={},
                extra_instructions=[
                    "You are executing the outcome_v4 single-agent smoke path.",
                    "Use exactly one execution environment for the entire task.",
                    "Return strict JSON only and include `workspace_exports` plus `artifact_refs`.",
                ],
                tool_call_limit=4,
            ),
            ledger=ledger,
            tool_traces=tool_traces,
            usage_records=model_usage,
            call_kind="single_agent",
            actor_id=identity.id,
        )
        exported_paths: list[str] = []
        response_payload: dict[str, Any] = {}
        cleanup_results: list[dict[str, Any]] = []
        staging_records: list[dict[str, Any]] = []
        primary_error: BaseException | None = None
        try:
            response = await agent.arun(_single_agent_prompt(prompt_text, scenario))
            response_payload = _normalize_response_payload(response)
            _assert_browser_render_evidence(
                scenario,
                events,
                context="single_agent",
            )
            exported_paths = _apply_workspace_exports(
                workspace_dir,
                response_payload,
                required_paths=scenario.output_contract.required_paths,
                durable_export_roots=[workspace_dir / "_provider_artifacts" / "single_agent"],
            )
        except BaseException as exc:
            primary_error = exc
        finally:
            try:
                staging_records = await _record_staging_activity(events, ledger, tool_traces)
                cleanup_results = [dict(item) for item in toolkit.close_all()]
                cleanup_steps.extend(_cleanup_steps_from_close_results(cleanup_results))
                _write_cleanup_manifest(workspace_dir, cleanup_steps, exported_paths)
            except BaseException as cleanup_exc:
                _record_cleanup_failure(
                    workspace_dir,
                    cleanup_error=cleanup_exc,
                    cleanup_steps=cleanup_steps,
                    cleanup_results=cleanup_results,
                    exported_paths=exported_paths,
                )
                if primary_error is None:
                    raise
        if primary_error is not None:
            raise primary_error

        _assert_required_outputs_present(workspace_dir, scenario.output_contract.required_paths)
        return FakeAdapterResult(
            raw_outputs={
                "mode": "single_agent",
                "provider": settings.provider,
                "model": settings.active_model,
                "tool_ids": _single_agent_tool_ids(scenario.scenario_id),
                "model_usage": model_usage,
                "events": events,
                "staging": staging_records,
                "cleanup_results": cleanup_results,
                "response": response_payload,
                "exported_paths": exported_paths,
                "fairness": fairness,
            },
            cleanup_evidence=["cleanup/cleanup.json"],
            tool_traces=tool_traces,
        )


class ProviderBackedSocietyAdapter:
    """Development smoke adapter that composes and runs real Agno specialists."""

    deterministic = False
    name = "provider-society"

    def __init__(
        self,
        *,
        settings_factory: Callable[[RetryPolicy], Settings] | None = None,
        preflight_fn: Callable[[Settings], dict[str, Any]] = model_capability_preflight,
        team_provider: Any | None = None,
        selection_provider: Any | None = None,
        composition_runtime_factory: Callable[..., Any] = CompositionRuntime,
        agentbay_toolkit_factory: Callable[..., Any] = AgentBayTools,
    ) -> None:
        self._settings_factory = settings_factory or _default_provider_settings
        self._preflight_fn = preflight_fn
        self._team_provider = team_provider
        self._selection_provider = selection_provider
        self._composition_runtime_factory = composition_runtime_factory
        self._agentbay_toolkit_factory = agentbay_toolkit_factory

    async def run(
        self,
        *,
        scenario: PublicScenarioSurface,
        workspace_dir: Path,
        prompt_text: str,
        ledger: AtomicBudgetLedger,
        retry_policy: RetryPolicy,
    ) -> FakeAdapterResult:
        settings = self._settings_factory(retry_policy)
        runtime_events: list[dict[str, Any]] = []
        tool_traces: list[ToolTraceRecord] = []
        model_usage: list[dict[str, Any]] = []
        durable_export_roots: list[Path] = []

        runtime = self._composition_runtime_factory(
            settings,
            workspace_dir / "_provider_artifacts" / "society",
            event_sink=lambda event_type, payload: runtime_events.append({"event_type": event_type, **dict(payload)}),
            preflight_fn=self._preflight_fn,
            build_agent_factory=_budgeted_assignment_agent_factory(ledger, tool_traces, model_usage),
            agentbay_toolkit_factory=_budgeted_agentbay_toolkit_factory(
                ledger,
                tool_traces,
                toolkit_factory=self._agentbay_toolkit_factory,
                workspace_source_dir=workspace_dir,
                owned_export_roots=durable_export_roots,
            ),
        )
        availability = runtime.available_tool_ids()
        typed_blockers = _provider_blockers_for_scenario(
            scenario,
            preflight=self._preflight_fn(settings),
            available_tool_ids=availability.tool_ids,
        )
        if typed_blockers:
            raise ProviderAdapterRefusal.from_blockers(typed_blockers)

        user_request = _society_user_request(prompt_text, scenario)
        context = runtime.build_composition_context(
            f"outcome-v4-society-{scenario.scenario_id}",
            user_request,
            _acceptance_requirements_for_scenario(scenario.scenario_id),
            required_capabilities=_required_capabilities_for_scenario(scenario.scenario_id),
        )
        if self._team_provider is not None:
            # Explicit compatibility seam for historical adapter tests and
            # replay. New provider-backed development runs never enter it.
            composer = TeamComposer(
                self._team_provider,
                event_sink=lambda event_type, payload: runtime_events.append({"event_type": event_type, **dict(payload)}),
            )
            plan = (await composer.compose(context)).plan
        else:
            coordinator = FixedSpecialistCoordinator(
                availability.tool_ids,
                event_sink=lambda event_type, payload: runtime_events.append({"event_type": event_type, **dict(payload)}),
            )
            catalog = coordinator.list_specialists()
            runtime_events.append({
                "event_type": "specialist_catalog_listed",
                "templates": [item.model_dump(mode="json") for item in catalog],
            })
            provider = self._selection_provider or AgnoFixedSpecialistSelectionProvider(
                model=build_model(settings),
                agent_factory=_budgeted_constructor_factory(ledger, tool_traces, model_usage),
            )
            agent_required_paths = [
                path
                for path in scenario.output_contract.required_paths
                if path not in set(scenario.output_contract.required_cleanup_markers)
            ]
            resolved = await _resolve_provider_specialist_selection(
                provider=provider,
                coordinator=coordinator,
                context=context,
                catalog=catalog,
                required_artifacts=agent_required_paths,
            )

            if hasattr(runtime, "set_invocation_hook"):
                async def invoke_when_ready(node: WorkNode, assignment: TeamAssignment) -> None:
                    await coordinator.invoke_specialist(
                        InvokeSpecialistCall(assignment_id=assignment.id),
                        satisfied_dependency_ids=node.depends_on,
                    )

                runtime.set_invocation_hook(invoke_when_ready)
            plan = resolved.plan

        fairness = _society_fairness_proof(scenario.scenario_id, plan.assignments)
        _assert_fairness_alignment(society_proof=fairness)
        _assert_browser_render_plan(plan, scenario.scenario_id)
        try:
            execution = await runtime.execute_plan(
                plan,
                context.task_id,
                user_request,
            )
        except BaseException as exc:
            cleanup_steps = _cleanup_steps_from_runtime_events(runtime_events)
            _write_cleanup_manifest(workspace_dir, cleanup_steps, [])
            _write_society_interruption_diagnostic(
                workspace_dir,
                plan=plan,
                runtime_events=runtime_events,
                error=exc,
            )
            raise
        diagnostic_path = _write_society_execution_diagnostic(
            workspace_dir,
            plan=plan,
            execution=execution,
            runtime_events=runtime_events,
        )
        cleanup_steps = _cleanup_steps_from_runtime_events(runtime_events)
        _write_cleanup_manifest(workspace_dir, cleanup_steps, [])
        staging_records = await _record_staging_activity(runtime_events, ledger, tool_traces)
        _assert_completed_society_graph(execution.graph_result, diagnostic_path)
        _assert_browser_render_evidence(
            scenario,
            runtime_events,
            context="society",
        )
        exported_paths = _apply_workspace_exports(
            workspace_dir,
            {"node_outputs": execution.node_outputs},
            required_paths=scenario.output_contract.required_paths,
            durable_export_roots=durable_export_roots,
        )
        _write_cleanup_manifest(workspace_dir, cleanup_steps, exported_paths)
        _assert_required_outputs_present(workspace_dir, scenario.output_contract.required_paths)

        return FakeAdapterResult(
            raw_outputs={
                "mode": "society",
                "provider": settings.provider,
                "model": settings.active_model,
                "model_usage": model_usage,
                "events": runtime_events,
                "staging": staging_records,
                "plan": plan.model_dump(mode="json"),
                "materialized_agents": [agent.model_dump(mode="json") for agent in execution.materialized_agents],
                "node_outputs": execution.node_outputs,
                "availability_blockers": [blocker.model_dump(mode="json") for blocker in execution.availability_blockers],
                "granted_tool_ids_by_assignment": fairness["assignment_grants"],
                "exported_paths": exported_paths,
                "fairness": fairness,
            },
            cleanup_evidence=["cleanup/cleanup.json"],
            tool_traces=tool_traces,
            graph_result=execution.graph_result.model_dump(mode="json"),
        )


@dataclass(slots=True)
class ProviderAdapterRefusal(RuntimeError):
    """Typed development refusal surfaced before any live provider calls."""

    reasons: list[str]
    typed_blockers: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_blockers(cls, blockers: list[dict[str, Any]]) -> "ProviderAdapterRefusal":
        reasons = [str(item.get("reason") or item.get("code") or "provider adapter refused") for item in blockers]
        return cls(reasons=reasons, typed_blockers=blockers)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, "; ".join(self.reasons))


async def _resolve_provider_specialist_selection(
    *,
    provider: Any,
    coordinator: FixedSpecialistCoordinator,
    context: Any,
    catalog: Any,
    required_artifacts: list[str],
) -> Any:
    """Spend at most one leader correction on schema or policy rejection."""

    prior_blockers: list[dict[str, Any]] = []
    for attempt_number in (1, 2):
        try:
            selection = await provider.propose(
                task_summary=context.task_summary,
                user_request=context.user_request,
                acceptance_requirements=context.acceptance_requirements,
                catalog=catalog,
                required_artifacts=required_artifacts,
                prior_blockers=prior_blockers,
                attempt_number=attempt_number,
            )
            return await coordinator.select_specialists(
                selection,
                task_summary=context.task_summary,
                required_capabilities=context.required_capabilities,
                required_artifacts=required_artifacts,
                required_acceptance_requirements=context.acceptance_requirements,
            )
        except SpecialistSelectionError as exc:
            prior_blockers = [item.model_dump(mode="json") for item in exc.blockers]
            if attempt_number == 2:
                raise ProviderAdapterRefusal.from_blockers(prior_blockers) from exc
        except FixedSpecialistProviderError as exc:
            prior_blockers = [{
                "code": "fixed_specialist_provider_malformed",
                "reason": str(exc)[:1000],
            }]
            if attempt_number == 2:
                raise ProviderAdapterRefusal.from_blockers(prior_blockers) from exc
    raise ProviderAdapterRefusal(["fixed specialist selection returned no plan"])


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_provider_settings(retry_policy: RetryPolicy) -> Settings:
    """Load the real backend settings with the frozen development overrides."""

    env_path = _backend_dir() / ".env"
    return Settings(
        _env_file=env_path,
        LLM_PROVIDER="qwen",
        QWEN_MODEL="qwen3.7-plus",
        TEAM_COMPOSITION_EXECUTION_ENABLED=True,
        SUBTASK_MAX_ATTEMPTS=retry_policy.max_attempts,
        SUBTASK_BACKOFF_BASE_SECONDS=retry_policy.backoff_base_seconds,
        SUBTASK_BACKOFF_CAP_SECONDS=retry_policy.backoff_cap_seconds,
        SOCIETY_MAX_MODEL_WORKERS=4,
        SOCIETY_MAX_AGENTBAY_SESSIONS=4,
        SOCIETY_MAX_MEDIA_JOBS=0,
        AGENTBAY_SESSION_TIMEOUT_SECONDS=120,
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _usage_record(response: Any, *, call_kind: str, actor_id: str) -> dict[str, Any]:
    """Extract truthful model usage without fabricating unavailable metrics."""

    record: dict[str, Any] = {
        "call_kind": call_kind,
        "actor_id": actor_id,
        "model": getattr(response, "model", None) or getattr(response, "model_id", None),
        "model_provider": getattr(response, "model_provider", None),
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "cost": None,
        "duration": 0.0,
        "time_to_first_token": 0.0,
        "metrics_present": False,
    }
    metrics = getattr(response, "metrics", None)
    raw = metrics.to_dict() if metrics is not None and callable(getattr(metrics, "to_dict", None)) else {}
    if isinstance(raw, dict) and raw:
        record["metrics_present"] = True
        record["model"] = raw.get("model") or raw.get("model_id") or record["model"]
        record["model_provider"] = raw.get("model_provider") or raw.get("provider") or record["model_provider"]
        record["input_tokens"] = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
        record["output_tokens"] = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
        record["total_tokens"] = int(raw.get("total_tokens") or (record["input_tokens"] + record["output_tokens"]))
        record["cache_read_tokens"] = int(raw.get("cache_read_input_tokens") or raw.get("cache_read_tokens") or 0)
        record["cache_write_tokens"] = int(raw.get("cache_creation_input_tokens") or raw.get("cache_write_tokens") or 0)
        record["reasoning_tokens"] = int(raw.get("reasoning_tokens") or 0)
        record["cost"] = raw.get("cost", raw.get("total_cost"))
        record["duration"] = float(raw.get("duration") or raw.get("response_time") or 0.0)
        record["time_to_first_token"] = float(raw.get("time_to_first_token") or raw.get("ttft") or 0.0)
    return record


def _usage_charge_delta(usage: dict[str, Any], usage_history: list[dict[str, Any]]) -> dict[str, int | str]:
    """Normalize provider metrics into one truthful non-duplicated debit delta."""

    actor_id = str(usage.get("actor_id") or "")
    call_kind = str(usage.get("call_kind") or "")
    prior_records = [
        candidate
        for candidate in usage_history
        if str(candidate.get("actor_id") or "") == actor_id and str(candidate.get("call_kind") or "") == call_kind
    ]
    prior_input_max = max((int(candidate.get("input_tokens") or 0) for candidate in prior_records), default=0)
    prior_output_max = max((int(candidate.get("output_tokens") or 0) for candidate in prior_records), default=0)
    raw_input = int(usage.get("input_tokens") or 0)
    raw_output = int(usage.get("output_tokens") or 0)
    looks_cumulative = raw_input >= prior_input_max and raw_output >= prior_output_max and (
        raw_input > prior_input_max or raw_output > prior_output_max
    )
    if looks_cumulative and prior_records:
        debit_input = raw_input - prior_input_max
        debit_output = raw_output - prior_output_max
        normalization = "cumulative_delta"
    else:
        debit_input = raw_input
        debit_output = raw_output
        normalization = "direct"
    return {
        "input_tokens": max(0, debit_input),
        "output_tokens": max(0, debit_output),
        "total_tokens": max(0, debit_input + debit_output),
        "normalization": normalization,
    }


async def _debit_usage(ledger: AtomicBudgetLedger, usage: dict[str, Any], usage_history: list[dict[str, Any]]) -> None:
    """Charge provider usage once after normalizing cumulative metrics to deltas."""

    charge = _usage_charge_delta(usage, usage_history)
    usage["charged_input_tokens"] = int(charge["input_tokens"])
    usage["charged_output_tokens"] = int(charge["output_tokens"])
    usage["charged_total_tokens"] = int(charge["total_tokens"])
    usage["charge_normalization"] = str(charge["normalization"])
    if usage["charged_input_tokens"] > 0:
        await ledger.debit(
            "qwen_input_tokens",
            float(usage["charged_input_tokens"]),
            metadata={"usage_charge": "input", "normalization": usage["charge_normalization"]},
        )
    if usage["charged_output_tokens"] > 0:
        await ledger.debit(
            "qwen_output_tokens",
            float(usage["charged_output_tokens"]),
            metadata={"usage_charge": "output", "normalization": usage["charge_normalization"]},
        )


class _BudgetedAgentProxy:
    """Wrap an Agno agent so provider calls debit the shared benchmark ledger."""

    def __init__(
        self,
        agent: Any,
        *,
        ledger: AtomicBudgetLedger,
        tool_traces: list[ToolTraceRecord],
        usage_records: list[dict[str, Any]],
        call_kind: str,
        actor_id: str,
    ) -> None:
        self._agent = agent
        self._ledger = ledger
        self._tool_traces = tool_traces
        self._usage_records = usage_records
        self._call_kind = call_kind
        self._actor_id = actor_id

    async def arun(self, prompt: str) -> Any:
        method = getattr(self._agent, "arun", None) or getattr(self._agent, "run", None)
        if method is None:
            raise RuntimeError("provider-backed agent is missing run/arun")
        async with self._ledger.tool_call(
            "qwen_model_call",
            category="qwen_model_calls",
            amount=1.0,
            metadata={"call_kind": self._call_kind, "actor_id": self._actor_id},
        ) as trace:
            response = await _maybe_await(method(prompt))
            trace.finished_at = time.perf_counter()
            self._tool_traces.append(trace)
        usage = _usage_record(response, call_kind=self._call_kind, actor_id=self._actor_id)
        await _debit_usage(self._ledger, usage, self._usage_records)
        self._usage_records.append(usage)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


class _AsyncToolkitProxy:
    """Expose AgentBay-style tools with benchmark ledger accounting wrappers."""

    def __init__(self, toolkit: Any, ledger: AtomicBudgetLedger, tool_traces: list[ToolTraceRecord], family: str) -> None:
        self._toolkit = toolkit
        self._ledger = ledger
        self._tool_traces = tool_traces
        self._family = family
        self._wrapper_cache: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        cached = self._wrapper_cache.get(name)
        if cached is not None:
            return cached
        target = getattr(self._toolkit, name)
        if not callable(target):
            return target

        target_signature = inspect.signature(target)

        @functools.wraps(target)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started_at = time.perf_counter()
            category = "subprocess_seconds" if name in {"execute_command", "run_code"} else "qwen_model_calls"
            async with self._ledger.tool_call(
                name,
                category=category,
                amount=0.0,
                metadata={"family": self._family},
            ) as trace:
                result = await _maybe_await(target(*args, **kwargs))
                trace.finished_at = time.perf_counter()
                self._tool_traces.append(trace)
            if category == "subprocess_seconds":
                await self._ledger.debit("subprocess_seconds", round(max(0.0, time.perf_counter() - started_at), 6))
            return result

        # Agno/Pydantic build tool schemas from inspect.signature/get_type_hints.
        # Preserve the wrapped callable's public interface so no synthetic
        # args/kwargs fields leak into the generated schema.
        wrapper.__signature__ = target_signature
        wrapper.__annotations__ = dict(getattr(target, "__annotations__", {}))
        self._wrapper_cache[name] = wrapper
        return wrapper


class _SingleAgentToolkitFacade:
    """Bind the single-agent tool signatures to one hidden task ID and session."""

    def __init__(self, toolkit: Any, task_id: str) -> None:
        self._toolkit = toolkit
        self._task_id = task_id
        self._cached_start: dict[str, Any] | None = None
        artifact_root = getattr(toolkit, "_artifact_root", None) or getattr(toolkit, "artifact_root", None)
        self._local_artifacts = LocalArtifactTools(role_key="test_engineer", artifact_root=artifact_root) if artifact_root is not None else None

    async def start_execution_environment(self, purpose: str) -> dict[str, Any]:
        """Start or reuse the single execution environment for the task."""
        repeated_call = self._cached_start is not None
        if self._cached_start is None:
            self._cached_start = await _maybe_await(self._toolkit.start_execution_environment(self._task_id, purpose))
        repeated = dict(self._cached_start)
        if repeated.get("success") and isinstance(repeated.get("data"), dict):
            repeated["data"] = dict(repeated["data"])
            repeated["data"]["idempotent"] = repeated_call
        return repeated

    async def execute_command(self, handle: str, command_id: str, arguments: dict[str, Any] | None, timeout_seconds: int) -> dict[str, Any]:
        """Execute one approved command template inside the environment."""
        return await _maybe_await(self._toolkit.execute_command(handle, command_id, arguments, timeout_seconds))

    async def run_code(self, handle: str, language: str, code: str, timeout_seconds: int) -> dict[str, Any]:
        """Run a bounded code snippet inside the environment."""
        return await _maybe_await(self._toolkit.run_code(handle, language, code, timeout_seconds))

    async def read_text_file(self, handle: str, path: str, offset: int = 0, length: int = 65536) -> dict[str, Any]:
        """Read a bounded slice of a text file from the environment."""
        return await _maybe_await(self._toolkit.read_text_file(handle, path, offset, length))

    async def write_text_file(self, handle: str, path: str, content: str, mode: str = "overwrite") -> dict[str, Any]:
        """Write or append text content to a file in the environment."""
        return await _maybe_await(self._toolkit.write_text_file(handle, path, content, mode))

    async def list_files(self, handle: str, path: str) -> dict[str, Any]:
        """List files under one allowed environment path."""
        return await _maybe_await(self._toolkit.list_files(handle, path))

    async def export_artifact(self, handle: str, path: str, artifact_kind: str) -> dict[str, Any]:
        """Export one environment artifact into the benchmark artifact store."""
        return await _maybe_await(self._toolkit.export_artifact(handle, path, artifact_kind))

    async def inspect_artifact(self, artifact_ref: str) -> dict[str, Any]:
        """Inspect one task-local exported artifact."""
        if self._local_artifacts is None:
            return {
                "success": False,
                "error_code": "artifact_inspection_unavailable",
                "error_message": "Single-agent artifact inspection requires a durable artifact root.",
            }
        return self._local_artifacts.inspect_artifact(artifact_ref)

    async def report_independent_validation(
        self,
        passed: bool,
        checks: list[str],
        failures: list[str],
        inspected_artifact_refs: list[str],
        summary: str = "",
    ) -> dict[str, Any]:
        """Return a bounded local validation report."""
        if self._local_artifacts is None:
            return {
                "passed": bool(passed),
                "checks": list(checks),
                "failures": list(failures),
                "inspected_artifact_refs": list(inspected_artifact_refs),
                "summary": summary,
            }
        return self._local_artifacts.report_independent_validation(
            passed,
            checks,
            failures,
            inspected_artifact_refs,
            summary,
        )

    async def browser_render(self, handle: str, entry_html_path: str = "/workspace/app/dist/index.html") -> dict[str, Any]:
        """Render the default app entrypoint in desktop and mobile viewports."""
        return await _maybe_await(self._toolkit.browser_render(handle, entry_html_path))

    async def close_execution_environment(self, handle: str) -> dict[str, Any]:
        """Close the execution environment handle after work is complete."""
        return await _maybe_await(self._toolkit.close_execution_environment(handle))


def _budgeted_constructor_factory(
    ledger: AtomicBudgetLedger,
    tool_traces: list[ToolTraceRecord],
    usage_records: list[dict[str, Any]],
) -> Callable[..., Any]:
    def factory(**kwargs: Any) -> _BudgetedAgentProxy:
        return _BudgetedAgentProxy(
            Agent(**kwargs),
            ledger=ledger,
            tool_traces=tool_traces,
            usage_records=usage_records,
            call_kind="team_composition",
            actor_id="team_composer",
        )

    return factory


def _budgeted_assignment_agent_factory(
    ledger: AtomicBudgetLedger,
    tool_traces: list[ToolTraceRecord],
    usage_records: list[dict[str, Any]],
) -> Callable[..., Any]:
    def factory(identity: SocietyAgent, settings: Settings, **kwargs: Any) -> _BudgetedAgentProxy:
        return _BudgetedAgentProxy(
            build_agno_agent(identity, settings, **kwargs),
            ledger=ledger,
            tool_traces=tool_traces,
            usage_records=usage_records,
            call_kind="assignment",
            actor_id=identity.id,
        )

    return factory


def _budgeted_agentbay_toolkit_factory(
    ledger: AtomicBudgetLedger,
    tool_traces: list[ToolTraceRecord],
    *,
    toolkit_factory: Callable[..., Any] = AgentBayTools,
    workspace_source_dir: Path | None = None,
    owned_export_roots: list[Path] | None = None,
) -> Callable[..., Any]:
    def factory(**kwargs: Any) -> _AsyncToolkitProxy:
        if workspace_source_dir is not None:
            kwargs.setdefault("workspace_source_dir", workspace_source_dir)
        artifact_root = kwargs.get("artifact_root")
        if owned_export_roots is not None and artifact_root is not None:
            owned_export_roots.append(Path(artifact_root).resolve())
        return _AsyncToolkitProxy(toolkit_factory(**kwargs), ledger, tool_traces, "agentbay")

    return factory


def _single_agent_tools(
    toolkit: Any,
    task_id: str,
    ledger: AtomicBudgetLedger,
    tool_traces: list[ToolTraceRecord],
    scenario_id: str,
) -> list[Any]:
    wrapped = _AsyncToolkitProxy(_SingleAgentToolkitFacade(toolkit, task_id), ledger, tool_traces, "agentbay")
    tools = [
        wrapped.start_execution_environment,
        wrapped.execute_command,
        wrapped.run_code,
        wrapped.read_text_file,
        wrapped.write_text_file,
        wrapped.list_files,
        wrapped.export_artifact,
        wrapped.inspect_artifact,
        wrapped.report_independent_validation,
        wrapped.close_execution_environment,
    ]
    if scenario_id == "screenshot_to_product":
        tools.insert(6, wrapped.browser_render)
    return tools


def _canonical_callable_tool_union(scenario_id: str) -> list[str]:
    base = [
        "start_execution_environment",
        "execute_command",
        "run_code",
        "read_text_file",
        "write_text_file",
        "list_files",
        "export_artifact",
        "close_execution_environment",
        "inspect_artifact",
        "report_independent_validation",
    ]
    if scenario_id == "screenshot_to_product":
        base.append("browser_render")
    return base


def _single_agent_tool_ids(scenario_id: str) -> list[str]:
    return _canonical_callable_tool_union(scenario_id)


def _hash_tool_ids(tool_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(dict.fromkeys(tool_ids)), sort_keys=True).encode("utf-8")).hexdigest()


def _single_agent_fairness_proof(scenario_id: str, tool_names: list[str]) -> dict[str, Any]:
    canonical_union = sorted(dict.fromkeys(_canonical_callable_tool_union(scenario_id)))
    granted = sorted(dict.fromkeys(tool_names))
    return {
        "scenario_id": scenario_id,
        "canonical_callable_tool_union": canonical_union,
        "canonical_union_hash": _hash_tool_ids(canonical_union),
        "single_agent_callable_tool_ids": granted,
        "single_agent_union_hash": _hash_tool_ids(granted),
        "single_agent_matches_full_union": granted == canonical_union,
    }


def _society_fairness_proof(scenario_id: str, assignments: list[TeamAssignment]) -> dict[str, Any]:
    canonical_union = sorted(dict.fromkeys(_canonical_callable_tool_union(scenario_id)))
    assignment_grants: dict[str, list[str]] = {}
    overgrants: dict[str, list[str]] = {}
    for assignment in assignments:
        granted = sorted(dict.fromkeys(tool_id for grant in assignment.tool_grants for tool_id in grant.tool_ids))
        assignment_grants[assignment.id] = granted
        extra = sorted(set(granted) - set(canonical_union))
        if extra:
            overgrants[assignment.id] = extra
    society_union = sorted(dict.fromkeys(tool_id for tool_ids in assignment_grants.values() for tool_id in tool_ids))
    missing = sorted(set(canonical_union) - set(society_union))
    return {
        "scenario_id": scenario_id,
        "canonical_callable_tool_union": canonical_union,
        "canonical_union_hash": _hash_tool_ids(canonical_union),
        "assignment_grants": assignment_grants,
        "assignment_grant_hashes": {assignment_id: _hash_tool_ids(tool_ids) for assignment_id, tool_ids in assignment_grants.items()},
        "society_callable_tool_union": society_union,
        "society_union_hash": _hash_tool_ids(society_union),
        "missing_from_society_union": missing,
        "overgrants": overgrants,
        "society_matches_full_union": not overgrants and not missing and society_union == canonical_union,
    }


def _assert_fairness_alignment(*, single_agent_proof: dict[str, Any] | None = None, society_proof: dict[str, Any] | None = None) -> None:
    if single_agent_proof is not None and not bool(single_agent_proof["single_agent_matches_full_union"]):
        raise ProviderAdapterRefusal.from_blockers([{
            "code": "single_agent_tool_union_mismatch",
            "category": "fairness_configuration_mismatch",
            "service": "benchmark_harness",
            "reason": "Development/provider run refused because the single-agent callable tool union diverged from the canonical scenario union.",
            "detail": single_agent_proof,
        }])
    if society_proof is not None and not bool(society_proof["society_matches_full_union"]):
        raise ProviderAdapterRefusal.from_blockers([{
            "code": "society_tool_union_mismatch",
            "category": "fairness_configuration_mismatch",
            "service": "benchmark_harness",
            "reason": "Development/provider run refused because society callable grants diverged from the canonical scenario union.",
            "detail": society_proof,
        }])


def _provider_blockers_for_scenario(
    scenario: PublicScenarioSurface,
    *,
    preflight: dict[str, Any],
    available_tool_ids: list[str],
) -> list[dict[str, Any]]:
    blockers = [dict(item) for item in preflight.get("typed_blockers", [])]
    if scenario.scenario_id == "screenshot_to_product":
        browser_service = dict(preflight.get("provider_services", {})).get("browser", {})
        if not browser_service.get("ready") or "browser_render" not in set(available_tool_ids):
            blockers.append({
                "code": "browser_render_runtime_unavailable",
                "category": "missing_system_capability",
                "service": "browser",
                "reason": "Scenario B requires the bounded AgentBay browser render path plus local Playwright CDP support for desktop and mobile screenshots.",
                "remediation": "Expose browser_render in the AgentBay runtime and ensure preflight reports provider_services.browser.ready before running the provider smoke.",
            })
    if preflight.get("provider") != "qwen":
        blockers.append({
            "code": "provider_not_qwen",
            "category": "missing_system_capability",
            "service": "qwen",
            "reason": "Outcome v4 provider-backed development runs are frozen to the Qwen provider path.",
            "remediation": "Configure LLM_PROVIDER=qwen for provider-backed development smokes.",
        })
    if preflight.get("model") != "qwen3.7-plus":
        blockers.append({
            "code": "model_not_qwen37plus",
            "category": "missing_system_capability",
            "service": "qwen",
            "reason": "Outcome v4 provider-backed development runs are frozen to qwen3.7-plus.",
            "remediation": "Configure QWEN_MODEL=qwen3.7-plus before running provider-backed smokes.",
        })
    if scenario.scenario_id == "incident_repair":
        required = {"start_execution_environment", "execute_command", "run_code", "write_text_file", "export_artifact", "close_execution_environment"}
        missing = sorted(required - set(available_tool_ids))
        if missing:
            blockers.append({
                "code": "incident_runtime_tools_missing",
                "category": "missing_system_capability",
                "service": "agentbay",
                "reason": f"Scenario A is missing required bounded runtime tools: {missing}",
                "remediation": "Expose the existing AgentBay execution and export tools before running the provider smoke.",
            })
    return blockers


def _assert_browser_render_plan(plan: TeamCompositionPlan, scenario_id: str) -> None:
    if scenario_id != "screenshot_to_product":
        return
    holders: list[str] = []
    forbidden: list[str] = []
    for assignment in plan.assignments:
        granted = {
            tool_id
            for grant in assignment.tool_grants
            for tool_id in grant.tool_ids
        }
        if "browser_render" not in granted:
            continue
        if assignment.agent_template_id not in {"frontend_engineer", "test_engineer"}:
            forbidden.append(f"{assignment.id}:{assignment.agent_template_id}")
            continue
        holders.append(assignment.id)
    if forbidden:
        raise ProviderAdapterRefusal([
            f"browser_render granted outside least-privilege frontend/test roles: {sorted(forbidden)}",
        ])
    if not holders:
        raise ProviderAdapterRefusal([
            "screenshot_to_product society plans must grant browser_render to at least one frontend_engineer or test_engineer assignment",
        ])


def _assert_browser_render_evidence(
    scenario: PublicScenarioSurface,
    events: list[dict[str, Any]],
    *,
    context: str,
) -> None:
    if scenario.scenario_id != "screenshot_to_product":
        return
    success_events = [event for event in events if event.get("event_type") == "agentbay_browser_render_succeeded"]
    if not success_events:
        raise ProviderAdapterRefusal([f"{context} screenshot_to_product run produced no agentbay_browser_render_succeeded event"])
    latest = success_events[-1]
    viewports = latest.get("viewports")
    if not isinstance(viewports, list):
        raise ProviderAdapterRefusal([f"{context} browser render event omitted viewport evidence"])
    names = {item.get("viewport_name") for item in viewports if isinstance(item, dict)}
    if names != {"desktop", "mobile"}:
        raise ProviderAdapterRefusal([f"{context} browser render event must include desktop and mobile viewports"])
    for item in viewports:
        if not isinstance(item, dict):
            raise ProviderAdapterRefusal([f"{context} browser render viewport evidence is malformed"])
        if not isinstance(item.get("geometry"), dict):
            raise ProviderAdapterRefusal([f"{context} browser render viewport evidence omitted geometry data"])
        if not isinstance(item.get("interaction"), dict):
            raise ProviderAdapterRefusal([f"{context} browser render viewport evidence omitted interaction data"])
        evidence_artifact = item.get("evidence_artifact")
        screenshot_artifact = item.get("screenshot_artifact")
        if not isinstance(evidence_artifact, dict) or not isinstance(screenshot_artifact, dict):
            raise ProviderAdapterRefusal([f"{context} browser render viewport evidence omitted exported artifacts"])


def _required_capabilities_for_scenario(scenario_id: str) -> list[str]:
    if scenario_id == "incident_repair":
        return ["implementation", "sandbox_execution", "artifact_export", "test_execution", "artifact_inspection"]
    return ["implementation", "ui_engineering", "browser_testing", "artifact_export", "test_execution"]


def _acceptance_requirements_for_scenario(scenario_id: str) -> list[str]:
    if scenario_id == "incident_repair":
        return ["artifact_diff_review", "independent_execution", "rollback_constraints", "diagnosis_evidence_accuracy"]
    return ["responsive_geometry", "accessibility", "artifact_provenance", "desktop_mobile_render_evidence"]


def _artifact_semantics_for_scenario(scenario_id: str) -> str:
    """Return shared, schema-neutral deliverable guidance for both provider modes.

    The guidance restates public scoring categories without exposing private
    evaluator field names, expected values, or sealed acceptance logic.
    """

    if scenario_id == "incident_repair":
        return (
            "Machine-readable incident artifacts must explicitly communicate: overall test status and executed-test count; "
            "retained truthful failure information in test and security reports; whether the reachable dependency issue is "
            "closed; cited public evidence IDs; changed file basenames; real command evidence; and the validated maximum "
            "rollback duration in minutes. A repeated job must be ignored with an explicit duplicate outcome rather than "
            "being processed again. Use concise top-level snake_case fields derived from those public concepts."
        )
    return (
        "Machine-readable product artifacts must explicitly communicate: build status; core-flow and API-contract status; "
        "desktop and mobile rendered hero geometry with viewport dimensions; critical accessibility violation count; public "
        "copy source IDs; hero asset dimensions, format, and content hash; and whether the asset manifest and both browser "
        "screenshots were recorded. Use concise nested snake_case fields derived from those public concepts."
    )


def _single_agent_prompt(prompt_text: str, scenario: PublicScenarioSurface) -> str:
    return (
        "Execution contract:\n"
        "- Start exactly one bounded execution environment and reuse it for the full task.\n"
        "- The public fixture workspace is already staged under /workspace; treat the fixture manifest as reference only.\n"
        "- Produce every required deliverable path listed in the output contract.\n"
        "- Export each scored output artifact and return strict JSON only.\n"
        "- JSON shape: {\"summary\": str, \"artifact_refs\": [str], \"workspace_exports\": [{\"workspace_relative_path\": str, \"artifact_ref\": str}], \"notes\": [str]}.\n"
        f"Required paths: {json.dumps(scenario.output_contract.required_paths, sort_keys=True)}\n"
        f"Artifact semantics: {_artifact_semantics_for_scenario(scenario.scenario_id)}\n"
        "Public benchmark prompt and fixture manifest follow:\n"
        f"{prompt_text}\n"
    )


def _society_user_request(prompt_text: str, scenario: PublicScenarioSurface) -> str:
    cleanup_markers = {
        str(path).replace("\\", "/")
        for path in scenario.output_contract.required_cleanup_markers
    }
    specialist_required_paths = [
        path
        for path in scenario.output_contract.required_paths
        if str(path).replace("\\", "/") not in cleanup_markers
    ]
    specialist_contract = scenario.output_contract.model_dump(mode="json")
    specialist_contract["required_paths"] = specialist_required_paths
    specialist_contract.pop("required_cleanup_markers", None)

    contract_start = "Output contract:\n"
    fixture_start = "\n\nPublic fixture manifest:\n"
    before_contract, separator, contract_and_fixtures = prompt_text.partition(contract_start)
    _original_contract, fixture_separator, fixtures = contract_and_fixtures.partition(fixture_start)
    if separator and fixture_separator:
        execution_prompt = (
            f"{before_contract}{contract_start}"
            f"{json.dumps(specialist_contract, indent=2, sort_keys=True)}"
            f"{fixture_separator}{fixtures}"
        )
    else:
        # Compatibility fallback for injected tests or callers that provide a
        # bounded prompt without the canonical benchmark section markers.
        execution_prompt = prompt_text

    return (
        "Execution contract:\n"
        "- Compose the smallest capable team that can create the scored artifacts with independent validation.\n"
        "- Every execution role must use bounded AgentBay sessions and export durable artifacts only.\n"
        "- Public fixture files are already staged under /workspace for any execution assignment that needs them.\n"
        "- Dependency outputs must stay compact and artifact-focused.\n"
        "- Cleanup evidence is finalized by the runtime after the work graph; specialists must neither create nor validate it.\n"
        "- The leader must assign ownership of every required non-cleanup path to an execution specialist.\n"
        "- Each producer must export every owned path and return workspace_exports entries shaped as "
        "{\"workspace_relative_path\": str, \"artifact_ref\": str}.\n"
        f"- Required paths: {json.dumps(specialist_required_paths, sort_keys=True)}\n"
        f"- Artifact semantics: {_artifact_semantics_for_scenario(scenario.scenario_id)}\n"
        "Public benchmark prompt and fixture manifest follow:\n"
        f"{execution_prompt}\n"
    )


def _normalize_response_payload(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, BaseModel):
        return dict(response.model_dump(mode="json"))
    if isinstance(response, dict):
        return dict(response)
    content = getattr(response, "content", None)
    if isinstance(content, dict):
        return dict(content)
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            decoded = json.loads(stripped)
            if isinstance(decoded, dict):
                return decoded
        return {"output_text": stripped}
    if isinstance(response, str):
        stripped = response.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            decoded = json.loads(stripped)
            if isinstance(decoded, dict):
                return decoded
        return {"output_text": stripped}
    return {"output_text": str(response)}


def _collect_workspace_exports(payload: dict[str, Any], required_paths: list[str]) -> list[dict[str, str]]:
    exports: list[dict[str, str]] = []
    direct = payload.get("workspace_exports")
    if isinstance(direct, list):
        for item in direct:
            if not isinstance(item, dict):
                continue
            rel = item.get("workspace_relative_path") or item.get("path")
            ref = item.get("artifact_ref") or item.get("source")
            if isinstance(rel, str) and rel and isinstance(ref, str) and ref:
                exports.append({"workspace_relative_path": rel, "artifact_ref": ref})
    node_outputs = payload.get("node_outputs")
    if isinstance(node_outputs, dict):
        for output in node_outputs.values():
            if isinstance(output, dict):
                exports.extend(_collect_workspace_exports(output, required_paths))
    if exports:
        return exports
    artifact_refs = payload.get("artifact_refs")
    if not isinstance(artifact_refs, list):
        return []
    by_name: dict[str, str] = {}
    for ref in artifact_refs:
        if isinstance(ref, str) and ref:
            by_name[Path(ref).name] = ref
    fallback: list[dict[str, str]] = []
    for relative_path in required_paths:
        match = by_name.get(Path(relative_path).name)
        if match:
            fallback.append({"workspace_relative_path": relative_path, "artifact_ref": match})
    return fallback


def _path_uses_symlink(path: Path, root: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == root or current.parent == current:
            return False
        current = current.parent


def _resolve_durable_export_source(artifact_ref: str, durable_export_roots: list[Path]) -> Path:
    candidate = Path(artifact_ref)
    if not candidate.exists():
        raise WorkspaceExportViolation(
            code="workspace_export_source_missing",
            message=f"workspace export source is missing: {artifact_ref}",
            detail={"artifact_ref": artifact_ref},
        )
    resolved = candidate.resolve(strict=True)
    for root in durable_export_roots:
        resolved_root = root.resolve()
        absolute_candidate = candidate.absolute()
        try:
            absolute_candidate.relative_to(resolved_root)
        except ValueError:
            pass
        else:
            if _path_uses_symlink(absolute_candidate, resolved_root):
                raise WorkspaceExportViolation(
                    code="workspace_export_symlink_rejected",
                    message=f"workspace export source uses a symlink: {artifact_ref}",
                    detail={"artifact_ref": artifact_ref, "durable_export_root": str(resolved_root)},
                )
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        if not resolved.is_file():
            raise WorkspaceExportViolation(
                code="workspace_export_source_not_file",
                message=f"workspace export source is not a file: {artifact_ref}",
                detail={"artifact_ref": artifact_ref, "durable_export_root": str(resolved_root)},
            )
        return resolved
    raise WorkspaceExportViolation(
        code="workspace_export_outside_owned_roots",
        message=f"workspace export source is outside the owned durable export roots: {artifact_ref}",
        detail={
            "artifact_ref": artifact_ref,
            "durable_export_roots": [str(root.resolve()) for root in durable_export_roots],
        },
    )


def _apply_workspace_exports(
    workspace_dir: Path,
    payload: dict[str, Any],
    *,
    required_paths: list[str],
    durable_export_roots: list[Path],
) -> list[str]:
    exported_paths: list[str] = []
    if not durable_export_roots:
        raise WorkspaceExportViolation(
            code="workspace_export_roots_missing",
            message="workspace export roots were not recorded for this attempt",
            detail={},
        )
    for item in _collect_workspace_exports(payload, required_paths):
        relative_path = item["workspace_relative_path"].replace("\\", "/")
        source = _resolve_durable_export_source(item["artifact_ref"], durable_export_roots)
        destination = (workspace_dir / relative_path).resolve()
        workspace_root = workspace_dir.resolve()
        if workspace_root != destination and workspace_root not in destination.parents:
            raise WorkspaceExportViolation(
                code="workspace_export_destination_escape",
                message=f"workspace export escapes fixture root: {relative_path}",
                detail={"workspace_relative_path": relative_path},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        exported_paths.append(relative_path)
    return sorted(dict.fromkeys(exported_paths))


async def _record_staging_activity(
    events: list[dict[str, Any]],
    ledger: AtomicBudgetLedger,
    tool_traces: list[ToolTraceRecord],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    existing_keys = {
        (
            trace.tool_id,
            trace.started_at,
            trace.finished_at,
            str(trace.metadata.get("source_tree_hash", "")),
        )
        for trace in tool_traces
    }
    for event in events:
        if event.get("event_type") != "agentbay_workspace_stage_succeeded":
            continue
        started_at = float(event.get("started_at") or 0.0)
        finished_at = float(event.get("finished_at") or started_at)
        duration_seconds = round(max(0.0, float(event.get("duration_seconds") or 0.0)), 6)
        metadata = {
            "role_key": event.get("role_key"),
            "source_tree_hash": event.get("source_tree_hash"),
            "staged_tree_hash": event.get("staged_tree_hash"),
            "file_count": event.get("file_count"),
            "total_bytes": event.get("total_bytes"),
        }
        dedupe_key = ("stage_workspace_inputs", started_at, finished_at, str(metadata["source_tree_hash"] or ""))
        if dedupe_key not in existing_keys:
            tool_traces.append(
                ToolTraceRecord(
                    tool_id="stage_workspace_inputs",
                    category="artifact_staging",
                    amount=duration_seconds,
                    started_at=started_at,
                    finished_at=finished_at,
                    metadata=metadata,
                )
            )
            existing_keys.add(dedupe_key)
        records.append(
            {
                "role_key": event.get("role_key"),
                "source_root": event.get("source_root"),
                "remote_root": event.get("remote_root"),
                "source_tree_hash": event.get("source_tree_hash"),
                "staged_tree_hash": event.get("staged_tree_hash"),
                "file_count": event.get("file_count"),
                "total_bytes": event.get("total_bytes"),
                "manifest": event.get("manifest", []),
                "verified_hashes": event.get("verified_hashes", {}),
                "request_ids": event.get("request_ids", []),
                "duration_seconds": duration_seconds,
            }
        )
    return records


def _assert_required_outputs_present(workspace_dir: Path, required_paths: list[str]) -> None:
    missing = [relative for relative in required_paths if not (workspace_dir / relative).exists()]
    if missing:
        raise RuntimeError(f"provider-backed execution did not materialize required outputs: {missing}")


def _cleanup_steps_from_close_results(close_results: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    for item in close_results:
        if item.get("success"):
            steps.append("agentbay_session_closed")
        else:
            message = item.get("error_message") or item.get("error_code") or "agentbay_cleanup_failed"
            steps.append(str(message))
    return steps or ["agentbay_cleanup_not_required"]


def _cleanup_steps_from_runtime_events(events: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    for item in events:
        if item.get("event_type") == "composition_assignment_cleanup_completed":
            steps.append("composition_assignment_cleanup_completed")
        if item.get("event_type") == "composition_assignment_cleanup_warning":
            steps.append("composition_assignment_cleanup_warning")
    return steps or ["composition_runtime_cleanup_not_required_or_observed"]


def _write_cleanup_manifest(workspace_dir: Path, cleanup_steps: list[str], exported_paths: list[str]) -> None:
    atomic_write_json(
        workspace_dir / "cleanup" / "cleanup.json",
        {
            "workspace_removed": False,
            "artifact_manifest_complete": bool(exported_paths),
            "cleanup_steps": cleanup_steps,
            "exported_paths": exported_paths,
        },
    )


def _write_society_execution_diagnostic(
    workspace_dir: Path,
    *,
    plan: TeamCompositionPlan,
    execution: Any,
    runtime_events: list[dict[str, Any]],
) -> Path:
    """Persist secret-safe work-graph evidence beside the development attempt."""

    event_counts: dict[str, int] = {}
    for event in runtime_events:
        event_type = str(event.get("event_type") or "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    failure_events = [
        {
            key: value
            for key, value in event.items()
            if key in {"event_type", "assignment_id", "node_id", "command_id", "error", "error_code", "error_message", "path"}
        }
        for event in runtime_events
        if any(marker in str(event.get("event_type") or "") for marker in ("failed", "rejected", "warning"))
    ][:50]
    path = workspace_dir.parent / "society_execution_diagnostic.json"
    atomic_write_json(
        path,
        {
            "plan": plan.model_dump(mode="json"),
            "graph_result": execution.graph_result.model_dump(mode="json"),
            "materialized_agents": [agent.model_dump(mode="json") for agent in execution.materialized_agents],
            "node_outputs": execution.node_outputs,
            "availability_blockers": [blocker.model_dump(mode="json") for blocker in execution.availability_blockers],
            "runtime_event_counts": dict(sorted(event_counts.items())),
            "runtime_failure_events": failure_events,
        },
    )
    return path


def _write_society_interruption_diagnostic(
    workspace_dir: Path,
    *,
    plan: TeamCompositionPlan,
    runtime_events: list[dict[str, Any]],
    error: BaseException,
) -> Path:
    """Persist bounded interruption and cleanup evidence when no graph result returns."""

    event_counts: dict[str, int] = {}
    for event in runtime_events:
        event_type = str(event.get("event_type") or "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    path = workspace_dir.parent / "society_execution_interruption.json"
    atomic_write_json(
        path,
        {
            "plan": plan.model_dump(mode="json"),
            "error_type": type(error).__name__,
            "message": str(error)[:1000],
            "runtime_event_counts": dict(sorted(event_counts.items())),
            "cleanup_observed": bool(
                event_counts.get("composition_assignment_cleanup_completed")
                or event_counts.get("composition_assignment_cleanup_warning")
            ),
        },
    )
    return path


def _assert_completed_society_graph(graph_result: Any, diagnostic_path: Path) -> None:
    """Fail at the graph boundary with actionable node codes and a diagnostic path."""

    if graph_result.terminal_status == WorkGraphTerminalStatus.COMPLETED:
        return
    failures: list[str] = []
    for node_id, record in graph_result.nodes.items():
        if str(record.status) == "completed":
            continue
        latest = record.attempts[-1] if record.attempts else None
        code = getattr(latest, "code", None) or "no_attempt_code"
        failures.append(f"{node_id}:{record.status}:{code}")
    summary = ", ".join(failures) or "no node failure details"
    raise RuntimeError(
        "provider-backed society graph did not complete: "
        f"terminal_status={graph_result.terminal_status}; nodes=[{summary}]; "
        f"diagnostic={diagnostic_path.name}"
    )


def _record_cleanup_failure(
    workspace_dir: Path,
    *,
    cleanup_error: BaseException,
    cleanup_steps: list[str],
    cleanup_results: list[dict[str, Any]],
    exported_paths: list[str],
) -> None:
    atomic_write_json(
        workspace_dir / "cleanup" / "cleanup_failure.json",
        {
            "error_type": type(cleanup_error).__name__,
            "message": str(cleanup_error),
            "cleanup_results": cleanup_results,
            "exported_paths": exported_paths,
        },
    )
    _write_cleanup_manifest(
        workspace_dir,
        [*cleanup_steps, "agentbay_cleanup_failure_recorded"],
        exported_paths,
    )


def _grant(capability: str, tool_ids: list[str]) -> ToolGrant:
    return ToolGrant(capability=capability, tool_ids=tool_ids)


def _build_society_plan(scenario_id: str) -> TeamCompositionPlan:
    if scenario_id == "incident_repair":
        return TeamCompositionPlan(
            task_summary="Repair the incident fixture with parallel specialists.",
            assignments=[
                TeamAssignment(id="ops", agent_template_id="researcher", objective="Read evidence.", required_capabilities=["evidence_gathering"], tool_grants=[_grant("evidence_gathering", ["read_fixture"])]),
                TeamAssignment(id="auth", agent_template_id="builder", objective="Repair auth.", required_capabilities=["implementation"], tool_grants=[_grant("implementation", ["filesystem_edit"])]),
                TeamAssignment(id="worker", agent_template_id="builder", objective="Repair worker.", required_capabilities=["implementation"], tool_grants=[_grant("implementation", ["filesystem_edit"])]),
                TeamAssignment(id="integration", agent_template_id="test_engineer", objective="Validate reports.", required_capabilities=["validation"], tool_grants=[_grant("validation", ["subprocess_check", "write_report"])]),
            ],
            work_graph=[
                WorkNode(id="ops", assignment_id="ops"),
                WorkNode(id="auth", assignment_id="auth", depends_on=["ops"]),
                WorkNode(id="worker", assignment_id="worker", depends_on=["ops"]),
                WorkNode(id="integration", assignment_id="integration", depends_on=["auth", "worker"]),
            ],
            selection_rationale="One evidence reader releases two independent repairs before integration.",
        )
    return TeamCompositionPlan(
        task_summary="Implement the product fixture with parallel frontend and asset work.",
        assignments=[
            TeamAssignment(id="design", agent_template_id="researcher", objective="Read brief and contract.", required_capabilities=["evidence_gathering"], tool_grants=[_grant("evidence_gathering", ["read_fixture"])]),
            TeamAssignment(id="frontend", agent_template_id="frontend_engineer", objective="Implement app.", required_capabilities=["ui_engineering"], tool_grants=[_grant("ui_engineering", ["filesystem_edit"])]),
            TeamAssignment(id="assets", agent_template_id="builder", objective="Prepare assets and screenshots.", required_capabilities=["implementation"], tool_grants=[_grant("implementation", ["copy_asset"])]),
            TeamAssignment(id="validation", agent_template_id="test_engineer", objective="Write deterministic reports.", required_capabilities=["validation"], tool_grants=[_grant("validation", ["subprocess_check", "write_report"])]),
        ],
        work_graph=[
            WorkNode(id="design", assignment_id="design"),
            WorkNode(id="frontend", assignment_id="frontend", depends_on=["design"]),
            WorkNode(id="assets", assignment_id="assets", depends_on=["design"]),
            WorkNode(id="validation", assignment_id="validation", depends_on=["frontend", "assets"]),
        ],
        selection_rationale="Analysis unlocks independent frontend and asset work before validation.",
    )


async def _incident_node_runner(node_id: str, tools: DeterministicToolSurface) -> dict[str, Any]:
    if node_id == "ops":
        await tools.read_text("public/evidence/logs.json")
        return {"artifact_refs": ["evidence/logs.json"]}
    if node_id == "auth":
        await tools.write_text("repo/services/api/auth.py", _REPAIRED_AUTH)
        return {"artifact_refs": ["repo/services/api/auth.py"]}
    if node_id == "worker":
        await asyncio.gather(
            tools.write_text("repo/services/worker/idempotency.py", _REPAIRED_WORKER),
            tools.write_text("repo/services/config/database.py", _REPAIRED_DATABASE),
        )
        await tools.write_text("repo/requirements.txt", "pyyaml==6.0.2\n")
        return {"artifact_refs": ["repo/services/worker/idempotency.py", "repo/services/config/database.py", "repo/requirements.txt"]}
    await _write_incident_reports(tools)
    return {"artifact_refs": ["reports/test_report.json", "reports/security_report.json", "reports/evidence_report.json"]}


async def _product_node_runner(node_id: str, tools: DeterministicToolSurface) -> dict[str, Any]:
    if node_id == "design":
        await tools.read_text("public/component_contract.json")
        return {"artifact_refs": ["public/component_contract.json"]}
    if node_id == "frontend":
        await tools.write_text("app/src/app.js", _APP_JS)
        await tools.write_text("app/src/styles.css", _APP_CSS)
        await tools.write_text("app/dist/index.html", _DIST_HTML)
        return {"artifact_refs": ["app/src/app.js", "app/src/styles.css", "app/dist/index.html"]}
    if node_id == "assets":
        await asyncio.gather(
            tools.copy_file("public/references/desktop.png", "screenshots/desktop.png"),
            tools.copy_file("public/references/mobile.png", "screenshots/mobile.png"),
            tools.copy_file("public/assets/hero-card.png", "app/dist/assets/hero-card.png"),
        )
        return {"artifact_refs": ["screenshots/desktop.png", "screenshots/mobile.png", "app/dist/assets/hero-card.png"]}
    await _write_product_reports(tools)
    return {"artifact_refs": ["reports/build.json", "reports/interaction.json", "reports/layout.json", "reports/a11y.json", "reports/copy_manifest.json", "reports/provenance.json"]}


async def _materialize_incident_solution(tools: DeterministicToolSurface) -> None:
    await asyncio.gather(
        tools.read_text("public/evidence/logs.json"),
        tools.read_text("public/repo_manifest.json"),
    )
    await tools.write_text("repo/services/api/auth.py", _REPAIRED_AUTH)
    await asyncio.gather(
        tools.write_text("repo/services/worker/idempotency.py", _REPAIRED_WORKER),
        tools.write_text("repo/services/config/database.py", _REPAIRED_DATABASE),
    )
    await tools.write_text("repo/requirements.txt", "pyyaml==6.0.2\n")
    await _write_incident_reports(tools)


async def _materialize_product_solution(tools: DeterministicToolSurface) -> None:
    await asyncio.gather(
        tools.read_text("public/product_brief.json"),
        tools.read_text("public/component_contract.json"),
    )
    await tools.write_text("app/src/app.js", _APP_JS)
    await tools.write_text("app/src/styles.css", _APP_CSS)
    await tools.write_text("app/dist/index.html", _DIST_HTML)
    await asyncio.gather(
        tools.copy_file("public/references/desktop.png", "screenshots/desktop.png"),
        tools.copy_file("public/references/mobile.png", "screenshots/mobile.png"),
        tools.copy_file("public/assets/hero-card.png", "app/dist/assets/hero-card.png"),
    )
    await _write_product_reports(tools)


async def _write_incident_reports(tools: DeterministicToolSurface) -> None:
    await tools.record_subprocess_seconds(2.5, "pytest -q")
    await tools.write_json("reports/test_report.json", {
        "passed": True,
        "tests_run": 6,
        "truthful_failures_retained": True,
    })
    await tools.write_json("reports/security_report.json", {
        "passed": True,
        "reachable_vulnerability_closed": True,
        "truthful_failures_retained": True,
    })
    await tools.write_json("reports/evidence_report.json", {
        "evidence_ids": ["AUTH-LOG-401", "DB-CONFIG-URL", "JOB-TRACE-008"],
        "changed_files": ["auth.py", "idempotency.py", "database.py", "requirements.txt"],
        "command_evidence": ["pytest -q", "pip-audit --strict"],
    })
    await tools.write_json("incident/rollback_plan.json", {
        "max_rollback_minutes": 15,
        "validated": True,
    })
    await tools.write_json("cleanup/cleanup.json", {
        "workspace_removed": False,
        "artifact_manifest_complete": True,
        "cleanup_steps": ["kept deterministic workspace copy for audit"],
    })


async def _write_product_reports(tools: DeterministicToolSurface) -> None:
    await tools.record_subprocess_seconds(3.0, "npm run build")
    await tools.write_json("reports/build.json", {"passed": True})
    await tools.write_json("reports/interaction.json", {"core_flow_passed": True, "api_contract_passed": True})
    await tools.write_json("reports/layout.json", {
        "desktop": {"hero_width": 960, "viewport": [1440, 900]},
        "mobile": {"hero_width": 320, "viewport": [390, 844]},
    })
    await tools.write_json("reports/a11y.json", {"critical_violations": 0})
    await tools.write_json("reports/copy_manifest.json", {
        "source_ids": ["COPY-HERO-001", "COPY-FEATURE-002", "COPY-CTA-003"],
    })
    hero_hash = json.loads((tools.workspace_dir / "public" / "asset_manifest.json").read_text(encoding="utf-8"))["hero_sha256"]
    await tools.write_json("reports/provenance.json", {
        "hero_asset": {"dimensions": [256, 144], "format": "png", "sha256": hero_hash},
        "asset_manifest_complete": True,
        "screenshots_recorded": True,
    })
    await tools.write_json("cleanup/cleanup.json", {
        "workspace_removed": False,
        "artifact_manifest_complete": True,
        "cleanup_steps": ["kept deterministic workspace copy for audit"],
    })


_REPAIRED_AUTH = """def allow_support_scope(scope: str, token_enabled: bool) -> bool:
    \"\"\"Return whether the support scope is allowed for enabled tokens only.\"\"\"

    return token_enabled and scope in {\"admin\", \"support\"}
"""

_REPAIRED_WORKER = """processed_ids: set[str] = set()


def process_job(job_id: str) -> str:
    \"\"\"Return a stable idempotent worker result.\"\"\"

    if job_id in processed_ids:
        return \"duplicate_ignored\"
    processed_ids.add(job_id)
    return \"processed\"
"""

_REPAIRED_DATABASE = """import os


def database_url() -> str:
    \"\"\"Return the configured database URL for the fixture.\"\"\"

    return os.getenv(\"DB_URL\", \"postgresql://localhost/devdb\")
"""

_APP_JS = """export function renderHero() {
  return {
    title: \"Ship calmer incident tooling\",
    subtitle: \"A compact control plane for repair, rollout, and rollback.\",
    cta: \"Review the launch checklist\"
  };
}
"""

_APP_CSS = """:root {
  color-scheme: light;
  --bg: #f5efe4;
  --ink: #1a1b1f;
  --accent: #c74b32;
}

body {
  margin: 0;
  background: linear-gradient(180deg, #f5efe4 0%, #f2d8c7 100%);
  color: var(--ink);
  font-family: Georgia, serif;
}
"""

_DIST_HTML = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Layer B Fixture</title>
    <link rel=\"stylesheet\" href=\"../src/styles.css\" />
  </head>
  <body>
    <main>
      <h1>Ship calmer incident tooling</h1>
      <p>A compact control plane for repair, rollout, and rollback.</p>
      <button>Review the launch checklist</button>
      <img src=\"assets/hero-card.png\" alt=\"Product hero card\" />
    </main>
  </body>
</html>
"""
