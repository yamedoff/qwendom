"""Credential-aware Phase 4 composition runtime without orchestrator wiring."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from config import Settings
from society.agents import build_agno_agent, researcher_uses_context7
from society.capability_registry import canonical_tool_id, get_role_capabilities, resolve_specialist_bundle
from society.models import SocietyAgent
from society.provider_preflight import model_capability_preflight
from society.schemas.team_composition import TeamAssignment, TeamCompositionPlan, WorkNode
from society.team_composer import CompositionContext, limits_from_settings
from society.tools.capabilities import execute_notes_demo_tool, risk_assessment_tool
from society.tools.image_generation import ImageGenerationTools
from society.tools.local_artifacts import LocalArtifactTools
from society.tools.media_store import MediaArtifactStore
from society.tools.video_generation import VideoGenerationTools
from society.work_graph import (
    BoundedWorkGraphExecutor,
    NodeExecutionError,
    RetrySettings,
    WorkGraphExecutionResult,
)

EventSink = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]
InvocationHook = Callable[[WorkNode, TeamAssignment], Awaitable[None] | None]
PreflightFn = Callable[..., Mapping[str, Any]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

_MAX_PROMPT_TEXT = 8_000
_MAX_DEPENDENCY_OUTPUTS = 4
_MAX_OUTPUT_KEYS = 32
_MAX_OUTPUT_ITEMS = 16
_MAX_OUTPUT_DEPTH = 3
_MAX_OUTPUT_TEXT = 500
_MAX_INTERNAL_TOOL_TRACE = 64
_MAX_FIXED_ASSIGNMENT_TOOL_CALLS = 64
_MAX_EXECUTION_PURPOSE_LENGTH = 200
_URL_PATTERN = re.compile(r"https?://\S+")

AGENTBAY_RUNTIME_TOOL_IDS: tuple[str, ...] = (
    "start_execution_environment",
    "execute_command",
    "run_code",
    "read_text_file",
    "write_text_file",
    "list_files",
    "browser_render",
    "export_artifact",
    "close_execution_environment",
)
IMAGE_RUNTIME_TOOL_IDS: tuple[str, ...] = ("generate_images", "inspect_image", "publish_image")
VIDEO_RUNTIME_TOOL_IDS: tuple[str, ...] = (
    "submit_text_to_video",
    "get_video_job",
    "cancel_video_job",
    "collect_video",
    "inspect_video",
)
LOCAL_RUNTIME_TOOL_IDS: tuple[str, ...] = ("context7_lookup", "execute_notes_demo", "risk_assessment")
LOCAL_ARTIFACT_TOOL_IDS: tuple[str, ...] = ("inspect_artifact", "report_independent_validation")
PROVIDER_TOOL_IDS: frozenset[str] = frozenset(AGENTBAY_RUNTIME_TOOL_IDS + IMAGE_RUNTIME_TOOL_IDS + VIDEO_RUNTIME_TOOL_IDS)


class AvailabilityBlocker(BaseModel):
    """Secret-safe runtime blocker surfaced from static preflight only."""

    code: str
    category: str
    service: str
    reason: str
    remediation: str


class RuntimeAvailability(BaseModel):
    """Truthful runtime tool availability and the blockers behind omissions."""

    tool_ids: list[str] = Field(default_factory=list)
    blockers: list[AvailabilityBlocker] = Field(default_factory=list)


class MaterializedAgentSummary(BaseModel):
    """Public summary of one per-assignment local specialist identity."""

    id: str
    assignment_id: str
    template_id: str
    template_version: str | None = None
    tool_bundle_hash: str | None = None
    skills: list[dict[str, str]] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    can_vote: bool = False


class CompositionExecutionResult(BaseModel):
    """Secret-safe public result from executing a validated composition plan."""

    graph_result: WorkGraphExecutionResult
    materialized_agents: list[MaterializedAgentSummary] = Field(default_factory=list)
    node_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    node_artifact_refs: dict[str, list[str]] = Field(default_factory=dict)
    availability_blockers: list[AvailabilityBlocker] = Field(default_factory=list)


class MaterializedAssignmentAgent(BaseModel):
    """Runtime-only identity used for one assignment attempt."""

    summary: MaterializedAgentSummary
    identity: SocietyAgent
    skill_instructions: list[str] = Field(default_factory=list)


class AssignmentExecutor(Protocol):
    """Injected assignment executor used by the work-graph runtime."""

    async def __call__(
        self,
        *,
        node: WorkNode,
        assignment: TeamAssignment,
        materialized_agent: MaterializedAssignmentAgent,
        direct_dependency_outputs: Mapping[str, Mapping[str, Any]],
        attempt: int,
        cancellation_event: asyncio.Event,
    ) -> Mapping[str, Any] | None:
        """Execute one assignment attempt and return a bounded mapping result."""


def _emit_tool_name(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    metadata = getattr(tool, "tool_name", None)
    if isinstance(metadata, str) and metadata:
        return metadata
    return getattr(tool, "__name__", type(tool).__name__)


def _callable_tool_name(tool: Any) -> str:
    return canonical_tool_id(_emit_tool_name(tool))


def _tool_call_limit_for_assignment(assignment: TeamAssignment, tool_count: int) -> int:
    """Keep fixed specialist exploration bounded before contract finalization.

    Artifact finalization is enforced by the runtime after the model returns, so
    increasing this limit per artifact only encourages repeated exploration and
    can consume the entire scenario wall-time before validation begins.
    """

    legacy_limit = max(1, tool_count * 2 or 2)
    if assignment.template_version is None:
        return legacy_limit
    if assignment.agent_template_id == "test_engineer":
        return min(_MAX_FIXED_ASSIGNMENT_TOOL_CALLS, max(16, tool_count * 2))
    if assignment.agent_template_id == "builder":
        return min(_MAX_FIXED_ASSIGNMENT_TOOL_CALLS, max(16, tool_count * 2 + 2))
    return min(
        _MAX_FIXED_ASSIGNMENT_TOOL_CALLS,
        max(16, tool_count * 4),
    )


def _execute_notes_demo_available(settings: Settings) -> bool:
    return settings.role_specific_tools_enabled and _callable_tool_name(execute_notes_demo_tool) == "execute_notes_demo"


def _risk_assessment_available() -> bool:
    return _callable_tool_name(risk_assessment_tool) == "risk_assessment"


def _context7_available(settings: Settings) -> bool:
    researcher = SocietyAgent(id="researcher", name="Researcher", role="Researcher", skills=["evidence"])
    return callable(researcher_uses_context7) and researcher_uses_context7(researcher, settings)


def _canonical_granted_tool_ids(assignment: TeamAssignment) -> list[str]:
    granted: list[str] = []
    seen: set[str] = set()
    for grant in assignment.tool_grants:
        for tool_id in grant.tool_ids:
            canonical = canonical_tool_id(tool_id)
            if canonical in seen:
                continue
            seen.add(canonical)
            granted.append(canonical)
    return granted


def _build_assignment_identity(assignment: TeamAssignment) -> MaterializedAssignmentAgent:
    skill_instructions: list[str] = []
    skill_summaries: list[dict[str, str]] = []
    if assignment.template_version is not None:
        try:
            bundle = resolve_specialist_bundle(assignment.agent_template_id)
        except ValueError as exc:
            raise NodeExecutionError("capability", "specialist_skill_resolution_failed", str(exc)) from exc
        if bundle.template.version != assignment.template_version:
            raise NodeExecutionError(
                "capability",
                "specialist_template_version_mismatch",
                f"Template {assignment.agent_template_id!r} version drifted from "
                f"{assignment.template_version!r} to {bundle.template.version!r}.",
            )
        if bundle.tool_bundle_hash != assignment.tool_bundle_hash:
            raise NodeExecutionError(
                "capability",
                "specialist_tool_bundle_hash_mismatch",
                f"Template {assignment.agent_template_id!r} tool bundle hash does not match the persisted selection.",
            )
        if set(_canonical_granted_tool_ids(assignment)) != set(bundle.template.tool_ids):
            raise NodeExecutionError(
                "capability",
                "specialist_tool_bundle_override",
                f"Template {assignment.agent_template_id!r} must receive its exact predefined tool bundle.",
            )
        expected_skills = [(item.skill_id, item.version, item.sha256) for item in assignment.resolved_skills]
        actual_skills = [(item.skill_id, item.version, item.sha256) for item in bundle.skills]
        if expected_skills != actual_skills:
            raise NodeExecutionError(
                "capability",
                "specialist_skill_bundle_override",
                f"Template {assignment.agent_template_id!r} must receive its exact predefined skill bundle.",
            )
        skill_instructions = [item.content for item in bundle.skills]
        skill_summaries = [
            {"skill_id": item.skill_id, "version": item.version, "sha256": item.sha256}
            for item in bundle.skills
        ]
    registration = get_role_capabilities(assignment.agent_template_id)
    capabilities = list(registration.capabilities) if registration is not None else list(assignment.required_capabilities)
    local_id = f"assignment-{assignment.id}"
    name = assignment.agent_template_id.replace("_", " ").title()
    identity = SocietyAgent(
        id=local_id,
        name=f"{name} [{assignment.id}]",
        role=assignment.agent_template_id,
        skills=capabilities,
        parent_id=assignment.agent_template_id,
    )
    summary = MaterializedAgentSummary(
        id=local_id,
        assignment_id=assignment.id,
        template_id=assignment.agent_template_id,
        template_version=assignment.template_version,
        tool_bundle_hash=assignment.tool_bundle_hash,
        skills=skill_summaries,
        capabilities=capabilities,
        can_vote=False,
    )
    return MaterializedAssignmentAgent(
        summary=summary,
        identity=identity,
        skill_instructions=skill_instructions,
    )


def _bounded_text(value: Any, limit: int = _MAX_OUTPUT_TEXT) -> str:
    return str(value)[:limit]


def _bounded_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_OUTPUT_DEPTH:
        return _bounded_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_public_text(value[:_MAX_OUTPUT_TEXT])
    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        for key in list(value.keys())[:_MAX_OUTPUT_KEYS]:
            bounded[_bounded_text(key)] = _bounded_value(value[key], depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth + 1) for item in list(value)[:_MAX_OUTPUT_ITEMS]]
    if isinstance(value, BaseModel):
        return _bounded_value(value.model_dump(mode="json"), depth + 1)
    return _redact_public_text(_bounded_text(value))


def _redact_public_text(text: str) -> str:
    redacted = _URL_PATTERN.sub("[redacted-url]", text)
    redacted = re.sub(r"\b(session|provider_task|task_id|job_id|handle)[=:]\S+", r"\1=[redacted]", redacted, flags=re.IGNORECASE)
    return redacted


def _public_output(result: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(result or {})
    for key in ("prompt", "instructions", "session_id", "provider_task_id", "provider_result_url", "url"):
        raw.pop(key, None)
    return _bounded_value(raw)


def _tool_trace_entries(internal_trace: Sequence[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for item in internal_trace:
        if not isinstance(item, Mapping):
            continue
        event_type = item.get("event_type")
        payload = item.get("payload")
        if not isinstance(event_type, str) or not event_type:
            continue
        if not isinstance(payload, Mapping):
            payload = {}
        entries.append((event_type, payload))
    return entries


def _has_success_evidence(
    internal_trace: Sequence[Mapping[str, Any]],
    success_types: set[str],
) -> bool:
    return any(event_type in success_types for event_type, _payload in _tool_trace_entries(internal_trace))


def _has_unrecovered_failure(
    internal_trace: Sequence[Mapping[str, Any]],
    *,
    success_types: set[str],
    failure_types: set[str],
) -> bool:
    last_success_index: int | None = None
    last_failure_index: int | None = None
    for index, (event_type, _payload) in enumerate(_tool_trace_entries(internal_trace)):
        if event_type in success_types:
            last_success_index = index
        if event_type in failure_types:
            last_failure_index = index
    if last_failure_index is None:
        return False
    if last_success_index is None:
        return True
    return last_success_index < last_failure_index


def _is_validator_execution_success(event_type: str, payload: Mapping[str, Any]) -> bool:
    """Return whether an event proves validator work beyond repository compilation."""

    if event_type in {"agentbay_run_code_succeeded", "agentbay_browser_render_succeeded"}:
        return True
    return event_type == "agentbay_command_succeeded" and str(payload.get("command_id") or "") != "python_compile"


def _validator_has_execution_success(internal_trace: Sequence[Mapping[str, Any]]) -> bool:
    return any(_is_validator_execution_success(event_type, payload) for event_type, payload in _tool_trace_entries(internal_trace))


def _validator_has_unrecovered_execution_failure(internal_trace: Sequence[Mapping[str, Any]]) -> bool:
    last_success_index: int | None = None
    last_failure_index: int | None = None
    failure_types = {
        "agentbay_command_rejected",
        "agentbay_command_failed",
        "agentbay_run_code_rejected",
        "agentbay_run_code_failed",
        "agentbay_browser_render_failed",
        "agentbay_browser_render_rejected",
    }
    for index, (event_type, payload) in enumerate(_tool_trace_entries(internal_trace)):
        if _is_validator_execution_success(event_type, payload):
            last_success_index = index
        if event_type in failure_types:
            last_failure_index = index
    if last_failure_index is None:
        return False
    return last_success_index is None or last_success_index < last_failure_index


def _merge_unique_strings(*groups: Sequence[Any], limit: int = _MAX_OUTPUT_ITEMS) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, str):
                item = str(item)
            text = _redact_public_text(item[:_MAX_OUTPUT_TEXT]).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
            if len(merged) >= limit:
                return merged
    return merged


def _successful_event_payloads(
    internal_trace: Sequence[Mapping[str, Any]],
    event_type: str,
) -> list[Mapping[str, Any]]:
    return [payload for current_type, payload in _tool_trace_entries(internal_trace) if current_type == event_type]


def _event_backed_artifact_refs(
    internal_trace: Sequence[Mapping[str, Any]],
) -> list[str]:
    refs: list[str] = []
    for payload in _successful_event_payloads(internal_trace, "agentbay_artifact_exported"):
        artifact_ref = payload.get("artifact_ref")
        if isinstance(artifact_ref, Mapping):
            path = artifact_ref.get("path")
            if isinstance(path, str) and path.strip():
                refs.append(path)
                continue
            reference_id = artifact_ref.get("id")
            if isinstance(reference_id, str) and reference_id.strip():
                refs.append(reference_id)
        elif isinstance(artifact_ref, str) and artifact_ref.strip():
            refs.append(artifact_ref)
    return _merge_unique_strings(refs, limit=_MAX_OUTPUT_ITEMS)


def _event_backed_media_artifact_ids(
    internal_trace: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return media IDs reported by a successful generation-tool invocation.

    Media generation persists bytes in ``MediaArtifactStore`` rather than an
    AgentBay workspace.  Its result is emitted through the same bounded tool
    trace used for AgentBay evidence, so retain only IDs from successful calls;
    callers must still resolve each ID through the store before trusting it.
    """

    artifact_ids: list[str] = []
    for event_type, payload in _tool_trace_entries(internal_trace):
        if event_type != "composition_tool_event" or payload.get("success") is not True:
            continue
        data = payload.get("data")
        if not isinstance(data, Mapping):
            continue
        reported_ids = data.get("artifact_ids")
        if not isinstance(reported_ids, list):
            continue
        artifact_ids.extend(item for item in reported_ids if isinstance(item, str) and item.strip())
    return _merge_unique_strings(artifact_ids, limit=_MAX_OUTPUT_ITEMS)


def _merge_event_backed_output(
    assignment: TeamAssignment,
    result: Mapping[str, Any],
    internal_trace: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    merged = dict(_public_output(result))
    artifact_refs = _merge_unique_strings(
        merged.get("artifact_refs") if isinstance(merged.get("artifact_refs"), list) else [],
        _event_backed_artifact_refs(internal_trace),
        limit=_MAX_OUTPUT_ITEMS,
    )
    if artifact_refs:
        merged["artifact_refs"] = artifact_refs

    if assignment.agent_template_id != "test_engineer":
        return merged

    validation_events = _successful_event_payloads(internal_trace, "local_independent_validation_reported")
    if not validation_events:
        return merged

    latest_validation = validation_events[-1]
    if "passed" in latest_validation:
        merged["passed"] = bool(latest_validation.get("passed"))
    merged["checks"] = _merge_unique_strings(
        latest_validation.get("checks") if isinstance(latest_validation.get("checks"), list) else [],
        merged.get("checks") if isinstance(merged.get("checks"), list) else [],
        limit=_MAX_OUTPUT_ITEMS,
    )
    merged["inspected_artifact_refs"] = _merge_unique_strings(
        latest_validation.get("inspected_artifact_refs") if isinstance(latest_validation.get("inspected_artifact_refs"), list) else [],
        merged.get("inspected_artifact_refs") if isinstance(merged.get("inspected_artifact_refs"), list) else [],
        limit=_MAX_OUTPUT_ITEMS,
    )
    if isinstance(latest_validation.get("failures"), list):
        merged["failures"] = _merge_unique_strings(
            latest_validation.get("failures"),
            merged.get("failures") if isinstance(merged.get("failures"), list) else [],
            limit=_MAX_OUTPUT_ITEMS,
        )
    return merged


def _direct_dependency_outputs(node: WorkNode, node_outputs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    direct: dict[str, dict[str, Any]] = {}
    for dependency_id in list(node.depends_on)[:_MAX_DEPENDENCY_OUTPUTS]:
        output = node_outputs.get(dependency_id)
        if output is None:
            continue
        direct[dependency_id] = _bounded_value(output)
    return direct


def _normalize_agent_response(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, Mapping):
        return _public_output(response)
    if isinstance(response, BaseModel):
        return _public_output(response.model_dump(mode="json"))
    content = getattr(response, "content", None)
    if isinstance(content, Mapping):
        return _public_output(content)
    if isinstance(content, str):
        return {"output_text": _redact_public_text(content[:_MAX_PROMPT_TEXT])}
    if isinstance(response, str):
        stripped = response.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(decoded, Mapping):
                    return _public_output(decoded)
        return {"output_text": _redact_public_text(stripped[:_MAX_PROMPT_TEXT])}
    return {"output_text": _redact_public_text(_bounded_text(response, _MAX_PROMPT_TEXT))}


class AgnoAssignmentExecutor:
    """Default Phase 4 assignment executor backed by one Agno agent call."""

    def __init__(
        self,
        *,
        settings: Settings,
        artifact_root: Path,
        availability_provider: Callable[[], RuntimeAvailability],
        event_sink: EventSink | None = None,
        build_agent_factory: Callable[..., Any] = build_agno_agent,
        agentbay_toolkit_factory: Callable[..., Any] | None = None,
        image_toolkit_factory: Callable[..., Any] = ImageGenerationTools,
        video_toolkit_factory: Callable[..., Any] = VideoGenerationTools,
        media_store: MediaArtifactStore,
        http_client_factory: Callable[[], httpx.Client] = httpx.Client,
    ) -> None:
        self._settings = settings
        self._artifact_root = artifact_root
        self._availability_provider = availability_provider
        self._event_sink = event_sink or (lambda _event_type, _payload: None)
        self._build_agent_factory = build_agent_factory
        self._agentbay_toolkit_factory = agentbay_toolkit_factory or _lazy_agentbay_toolkit_factory
        self._image_toolkit_factory = image_toolkit_factory
        self._video_toolkit_factory = video_toolkit_factory
        self._media_store = media_store
        self._http_client_factory = http_client_factory
        self._current_user_request = ""

    def _toolkit_event_sink(
        self,
        assignment: TeamAssignment,
        node: WorkNode,
        internal_trace: list[dict[str, Any]],
    ) -> Callable[[dict[str, Any]], None]:
        def emit(payload: dict[str, Any]) -> None:
            event_type = str(payload.get("event_type") or "composition_tool_event")
            event_payload = dict(payload)
            event_payload.update({"assignment_id": assignment.id, "node_id": node.id})
            if len(internal_trace) < _MAX_INTERNAL_TOOL_TRACE:
                internal_trace.append(
                    {
                        "event_type": event_type,
                        "payload": _bounded_value(event_payload),
                    }
                )
            result = self._event_sink(event_type, event_payload)
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    pass

        return emit

    def _verified_generated_media_artifacts(
        self,
        internal_trace: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve generated-media trace IDs to durable, provenance-complete manifests."""

        verified: list[dict[str, Any]] = []
        for artifact_id in _event_backed_media_artifact_ids(internal_trace):
            try:
                manifest = self._media_store.load_artifact_manifest(artifact_id)
            except Exception:
                continue
            kind = manifest.get("kind")
            if kind not in {"image", "video"} or manifest.get("provenance_complete") is not True:
                continue
            verified.append({"artifact_id": artifact_id, "kind": kind})
        return verified

    @staticmethod
    def _missing_required_tool_evidence(
        assignment: TeamAssignment,
        granted_tool_ids: Sequence[str],
        internal_trace: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        missing: list[str] = []
        granted = set(granted_tool_ids)
        required_capabilities = set(assignment.required_capabilities)

        if "start_execution_environment" in granted and not _has_success_evidence(internal_trace, {"agentbay_start_succeeded"}):
            missing.append("start_execution_environment")
        if "sandbox_execution" in required_capabilities:
            has_sandbox_success = (
                _validator_has_execution_success(internal_trace)
                if assignment.agent_template_id == "test_engineer"
                else _has_success_evidence(
                    internal_trace,
                    {"agentbay_command_succeeded", "agentbay_run_code_succeeded", "agentbay_browser_render_succeeded"},
                )
            )
            if not has_sandbox_success:
                missing.append("sandbox_execution")
        if assignment.agent_template_id == "builder" and "export_artifact" in granted and not _has_success_evidence(
            internal_trace,
            {"agentbay_artifact_exported"},
        ):
            missing.append("export_artifact")
        if "browser_render" in granted and not _has_success_evidence(
            internal_trace,
            {"agentbay_browser_render_succeeded"},
        ):
            missing.append("browser_render")
        if assignment.agent_template_id == "test_engineer":
            if "inspect_artifact" in granted and not _has_success_evidence(internal_trace, {"local_artifact_inspected"}):
                missing.append("inspect_artifact")
            if "report_independent_validation" in granted and not _has_success_evidence(
                internal_trace,
                {"local_independent_validation_reported"},
            ):
                missing.append("report_independent_validation")
        return missing

    @staticmethod
    def _failed_required_tool_outcomes(
        assignment: TeamAssignment,
        granted_tool_ids: Sequence[str],
        internal_trace: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        granted = set(granted_tool_ids)
        required_capabilities = set(assignment.required_capabilities)
        failures: list[str] = []

        if "start_execution_environment" in granted and _has_unrecovered_failure(
            internal_trace,
            success_types={"agentbay_start_succeeded"},
            failure_types={"agentbay_start_rejected", "agentbay_start_failed"},
        ):
            failures.append("start_execution_environment")
        if "sandbox_execution" in required_capabilities:
            has_unrecovered_sandbox_failure = (
                _validator_has_unrecovered_execution_failure(internal_trace)
                if assignment.agent_template_id == "test_engineer"
                else _has_unrecovered_failure(
                    internal_trace,
                    success_types={"agentbay_command_succeeded", "agentbay_run_code_succeeded", "agentbay_browser_render_succeeded"},
                    failure_types={
                        "agentbay_command_rejected",
                        "agentbay_command_failed",
                        "agentbay_run_code_rejected",
                        "agentbay_run_code_failed",
                    },
                )
            )
            if has_unrecovered_sandbox_failure:
                failures.append("sandbox_execution")
        if assignment.agent_template_id == "builder" and "export_artifact" in granted and _has_unrecovered_failure(
            internal_trace,
            success_types={"agentbay_artifact_exported"},
            failure_types={"agentbay_artifact_export_failed"},
        ):
            failures.append("export_artifact")
        if "browser_render" in granted and _has_unrecovered_failure(
            internal_trace,
            success_types={"agentbay_browser_render_succeeded"},
            failure_types={"agentbay_browser_render_failed", "agentbay_browser_render_rejected"},
        ):
            failures.append("browser_render")
        if assignment.agent_template_id == "test_engineer" and "inspect_artifact" in granted and _has_unrecovered_failure(
            internal_trace,
            success_types={"local_artifact_inspected"},
            failure_types={"local_artifact_inspection_failed"},
        ):
            failures.append("inspect_artifact")
        return failures

    def set_user_request(self, user_request: str) -> None:
        """Store the bounded user request for the next assignment prompt."""

        self._current_user_request = _bounded_text(user_request, _MAX_PROMPT_TEXT)

    def _dependency_workspace_overlays(
        self,
        direct_dependency_outputs: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, str]:
        """Resolve durable producer exports that must overlay a dependant's sandbox workspace."""

        durable_root = (self._artifact_root / "agentbay").resolve()
        overlays: dict[str, str] = {}
        for dependency_id in sorted(direct_dependency_outputs):
            output = direct_dependency_outputs[dependency_id]
            exports = output.get("workspace_exports") if isinstance(output, Mapping) else None
            if not isinstance(exports, list):
                continue
            for item in exports:
                if not isinstance(item, Mapping):
                    continue
                relative_path = str(item.get("workspace_relative_path") or "").replace("\\", "/")
                artifact_ref = item.get("artifact_ref")
                relative = PurePosixPath(relative_path)
                if not relative_path or relative.is_absolute() or ".." in relative.parts:
                    raise NodeExecutionError("validation", "dependency_overlay_path_invalid", "Dependency export path is invalid")
                if not isinstance(artifact_ref, str) or not artifact_ref:
                    raise NodeExecutionError("validation", "dependency_overlay_ref_missing", "Dependency export is missing a durable artifact reference")
                candidate = Path(artifact_ref).resolve()
                try:
                    candidate.relative_to(durable_root)
                except ValueError as exc:
                    raise NodeExecutionError(
                        "validation",
                        "dependency_overlay_ref_untrusted",
                        "Dependency export is outside the composition artifact root",
                    ) from exc
                if not candidate.is_file() or candidate.is_symlink():
                    raise NodeExecutionError("validation", "dependency_overlay_ref_missing", "Dependency export is not a durable regular file")
                previous = overlays.get(relative.as_posix())
                if previous is not None and Path(previous).resolve() != candidate:
                    raise NodeExecutionError("validation", "dependency_overlay_conflict", "Dependencies exported conflicting workspace paths")
                overlays[relative.as_posix()] = str(candidate)
        return overlays

    def _verified_existing_exports(
        self,
        result: Mapping[str, Any],
        internal_trace: Sequence[Mapping[str, Any]],
        expected_artifacts: Sequence[str],
    ) -> dict[str, str]:
        """Return only durable, task-owned exports that map unambiguously to expected paths."""

        expected = {str(path).replace("\\", "/") for path in expected_artifacts}
        durable_root = (self._artifact_root / "agentbay").resolve()

        def verified_ref(value: Any) -> str | None:
            if not isinstance(value, str) or not value.strip():
                return None
            candidate = Path(value).resolve()
            if not candidate.is_file() or (candidate != durable_root and durable_root not in candidate.parents):
                return None
            return str(candidate)

        exports: dict[str, str] = {}
        direct = result.get("workspace_exports")
        if isinstance(direct, list):
            for item in direct:
                if not isinstance(item, Mapping):
                    continue
                relative_path = str(item.get("workspace_relative_path") or item.get("path") or "").replace("\\", "/")
                artifact_ref = verified_ref(item.get("artifact_ref"))
                if relative_path in expected and artifact_ref is not None:
                    exports[relative_path] = artifact_ref

        for event in internal_trace:
            if event.get("event_type") != "agentbay_artifact_exported":
                continue
            payload = event.get("payload")
            artifact = payload.get("artifact_ref") if isinstance(payload, Mapping) else None
            artifact_ref = verified_ref(artifact.get("path") if isinstance(artifact, Mapping) else None)
            if artifact_ref is None:
                continue
            file_name = Path(artifact_ref).name
            matches = [path for path in expected if file_name == Path(path).name or file_name.endswith(f"_{Path(path).name}")]
            if len(matches) == 1:
                exports.setdefault(matches[0], artifact_ref)
        return exports

    async def _enforce_required_artifact_exports(
        self,
        *,
        assignment: TeamAssignment,
        node: WorkNode,
        result: Mapping[str, Any],
        internal_trace: Sequence[Mapping[str, Any]],
        agentbay_toolkit: Any,
        cached_start_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Finalize every fixed producer artifact through its granted AgentBay export tool."""

        merged = dict(result)
        # Team plans may express sandbox artifacts as either
        # ``/workspace/path`` or workspace-relative ``path``.  Downstream
        # overlays must always receive the relative form.
        expected = list(
            dict.fromkeys(
                str(path).replace("\\", "/").removeprefix("/workspace/").lstrip("/")
                for path in assignment.expected_artifacts
            )
        )
        exports = self._verified_existing_exports(merged, internal_trace, expected)
        if not expected:
            return merged
        start_data = cached_start_result.get("data") if isinstance(cached_start_result, Mapping) else None
        handle = start_data.get("handle") if isinstance(start_data, Mapping) else None
        if not isinstance(handle, str) or not handle:
            raise NodeExecutionError(
                "validation",
                "required_artifact_export_environment_missing",
                "Cannot finalize required artifacts because no successful execution environment handle was recorded.",
            )

        exported_by_contract: list[str] = []
        for relative_path in expected:
            if relative_path in exports:
                continue
            suffix = Path(relative_path).suffix.lower()
            artifact_kind = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"} else "report" if suffix == ".json" else "code"
            export_result = await _maybe_await(
                agentbay_toolkit.export_artifact(handle, f"/workspace/{relative_path}", artifact_kind)
            )
            data = export_result.get("data") if isinstance(export_result, Mapping) else None
            artifact = data.get("artifact") if isinstance(data, Mapping) else None
            artifact_ref = artifact.get("path") if isinstance(artifact, Mapping) else None
            if not bool(export_result.get("success")) or not isinstance(artifact_ref, str):
                raise NodeExecutionError(
                    "validation",
                    "required_artifact_export_failed",
                    f"Required artifact could not be exported: {relative_path}",
                )
            exports[relative_path] = artifact_ref
            exported_by_contract.append(relative_path)

        merged["workspace_exports"] = [
            {"workspace_relative_path": relative_path, "artifact_ref": exports[relative_path]}
            for relative_path in expected
        ]
        merged["artifact_refs"] = _merge_unique_strings(
            merged.get("artifact_refs") if isinstance(merged.get("artifact_refs"), list) else [],
            [exports[path] for path in expected],
            limit=_MAX_OUTPUT_ITEMS,
        )
        await _emit_event(
            self._event_sink,
            "composition_required_artifacts_exported",
            {
                "assignment_id": assignment.id,
                "node_id": node.id,
                "artifact_paths": expected,
                "runtime_exported_paths": exported_by_contract,
            },
        )
        return merged

    async def _enforce_required_repository_check(
        self,
        *,
        assignment: TeamAssignment,
        node: WorkNode,
        internal_trace: Sequence[Mapping[str, Any]],
        agentbay_toolkit: Any,
        cached_start_result: Mapping[str, Any] | None,
    ) -> None:
        """Run the canonical real sandbox compile when a fixed producer omitted it."""

        if assignment.agent_template_id != "builder" or "sandbox_execution" not in assignment.required_capabilities:
            return
        if not any(str(path).replace("\\", "/").startswith("repo/") for path in assignment.expected_artifacts):
            return
        if _has_success_evidence(
            internal_trace,
            {"agentbay_command_succeeded", "agentbay_run_code_succeeded", "agentbay_browser_render_succeeded"},
        ):
            return
        start_data = cached_start_result.get("data") if isinstance(cached_start_result, Mapping) else None
        handle = start_data.get("handle") if isinstance(start_data, Mapping) else None
        if not isinstance(handle, str) or not handle:
            return
        check_result = await _maybe_await(
            agentbay_toolkit.execute_command(handle, "python_compile", {"target": "/workspace/repo"}, 30)
        )
        if not isinstance(check_result, Mapping) or check_result.get("success") is not True:
            message = check_result.get("error_message") if isinstance(check_result, Mapping) else "unknown failure"
            raise NodeExecutionError(
                "capability",
                "mandatory_repository_check_failed",
                f"Canonical repository compile failed: {_bounded_text(message, 1000)}",
            )
        await _emit_event(
            self._event_sink,
            "composition_required_repository_check_completed",
            {"assignment_id": assignment.id, "node_id": node.id, "command_id": "python_compile"},
        )

    async def __call__(
        self,
        *,
        node: WorkNode,
        assignment: TeamAssignment,
        materialized_agent: MaterializedAssignmentAgent,
        direct_dependency_outputs: Mapping[str, Mapping[str, Any]],
        attempt: int,
        cancellation_event: asyncio.Event,
    ) -> Mapping[str, Any] | None:
        if not self._settings.llm_enabled:
            raise NodeExecutionError("capability", "llm_unavailable", "LLM provider is not configured for composition execution")

        availability = self._availability_provider()
        available_tool_ids = set(availability.tool_ids)
        granted_tool_ids = _canonical_granted_tool_ids(assignment)
        unavailable = sorted(set(granted_tool_ids) - available_tool_ids)
        if unavailable:
            raise NodeExecutionError(
                "capability",
                "unavailable_tool_grant",
                f"Granted tools are not available in this runtime: {unavailable}",
            )

        tools: list[Any] = []
        warnings: list[str] = []
        agentbay_toolkit: Any | None = None
        media_client: httpx.Client | None = None
        tool_names: set[str] = set()
        internal_tool_trace: list[dict[str, Any]] = []
        cached_start_result: dict[str, Any] | None = None
        start_attempted = False
        sandbox_execution_attempts = 0
        sandbox_execution_successes = 0
        toolkit_event_sink = self._toolkit_event_sink(assignment, node, internal_tool_trace)
        dependency_workspace_overlays = self._dependency_workspace_overlays(direct_dependency_outputs)

        try:
            if any(tool_id in AGENTBAY_RUNTIME_TOOL_IDS for tool_id in granted_tool_ids):
                agentbay_toolkit = self._agentbay_toolkit_factory(
                    task_id=node.id,
                    role_key=assignment.agent_template_id,
                    allowed_remote_roots=["/workspace"],
                    artifact_root=self._artifact_root / "agentbay",
                    settings=self._settings,
                    event_sink=toolkit_event_sink,
                    workspace_overlay_exports=dependency_workspace_overlays,
                )

                def cached_execution_handle() -> str | None:
                    start_data = cached_start_result.get("data") if isinstance(cached_start_result, Mapping) else None
                    handle = start_data.get("handle") if isinstance(start_data, Mapping) else None
                    return handle if isinstance(handle, str) and handle else None

                for tool_id in granted_tool_ids:
                    if tool_id in AGENTBAY_RUNTIME_TOOL_IDS and hasattr(agentbay_toolkit, tool_id):
                        if tool_id == "start_execution_environment":
                            async def start_execution_environment(purpose: str) -> dict[str, Any]:
                                """Create an ephemeral execution environment for this assignment using only a bounded purpose."""

                                nonlocal cached_start_result, start_attempted
                                if start_attempted:
                                    if cached_start_result is None:
                                        return {"success": False, "error_code": "agentbay_start_missing_cache", "error_message": "Execution environment cache missing"}
                                    repeated = dict(cached_start_result)
                                    if repeated.get("success"):
                                        data = repeated.get("data")
                                        repeated["data"] = dict(data) if isinstance(data, Mapping) else {}
                                        repeated["data"]["idempotent"] = True
                                    return repeated
                                start_attempted = True
                                bounded_purpose = " ".join(str(purpose).split())[:_MAX_EXECUTION_PURPOSE_LENGTH].strip()
                                if not bounded_purpose:
                                    bounded_purpose = f"{assignment.agent_template_id} assignment"
                                cached_start_result = await _maybe_await(
                                    agentbay_toolkit.start_execution_environment(node.id, bounded_purpose)
                                )
                                return dict(cached_start_result)

                            start_execution_environment.__name__ = "start_execution_environment"
                            tool = start_execution_environment
                        elif tool_id == "close_execution_environment":
                            async def close_execution_environment(handle: str) -> dict[str, Any]:
                                """Defer physical deletion until exports and validation evidence are finalized."""

                                start_data = cached_start_result.get("data") if isinstance(cached_start_result, Mapping) else None
                                expected_handle = start_data.get("handle") if isinstance(start_data, Mapping) else None
                                if not isinstance(handle, str) or not handle or handle != expected_handle:
                                    return {
                                        "success": False,
                                        "error_code": "agentbay_close_rejected",
                                        "error_message": "Unknown execution environment handle",
                                    }
                                toolkit_event_sink({
                                    "event_type": "agentbay_close_deferred",
                                    "handle": handle,
                                    "reason": "runtime finalizes exports before cleanup",
                                })
                                return {
                                    "success": True,
                                    "data": {
                                        "handle": handle,
                                        "closed": False,
                                        "deferred": True,
                                    },
                                }

                            close_execution_environment.__name__ = "close_execution_environment"
                            tool = close_execution_environment
                        elif assignment.agent_template_id == "test_engineer" and tool_id == "execute_command":
                            async def execute_command(
                                handle: str,
                                command_id: str,
                                arguments: Mapping[str, Any] | None,
                                timeout_seconds: int,
                            ) -> dict[str, Any]:
                                """Run the validator's single combined sandbox check."""

                                nonlocal sandbox_execution_attempts, sandbox_execution_successes
                                if sandbox_execution_successes >= 1 or sandbox_execution_attempts >= 3:
                                    toolkit_event_sink({
                                        "event_type": "composition_sandbox_execution_limit_reached",
                                        "tool_id": "execute_command",
                                        "limit": 1,
                                    })
                                    return {
                                        "success": False,
                                        "error_code": "sandbox_execution_limit_reached",
                                        "error_message": "Validator sandbox execution limit reached; report existing evidence.",
                                    }
                                runtime_handle = cached_execution_handle()
                                if runtime_handle is None:
                                    return {
                                        "success": False,
                                        "error_code": "execution_environment_not_started",
                                        "error_message": "Start the execution environment before running validator checks.",
                                    }
                                sandbox_execution_attempts += 1
                                result = dict(await _maybe_await(
                                    agentbay_toolkit.execute_command(runtime_handle, command_id, arguments, timeout_seconds)
                                ))
                                if result.get("success") is True:
                                    sandbox_execution_successes += 1
                                return result

                            execute_command.__name__ = "execute_command"
                            tool = execute_command
                        elif assignment.agent_template_id == "test_engineer" and tool_id == "run_code":
                            async def run_code(handle: str, language: str, code: str, timeout_seconds: int) -> dict[str, Any]:
                                """Run the validator's single combined sandbox check."""

                                nonlocal sandbox_execution_attempts, sandbox_execution_successes
                                if sandbox_execution_successes >= 1 or sandbox_execution_attempts >= 3:
                                    toolkit_event_sink({
                                        "event_type": "composition_sandbox_execution_limit_reached",
                                        "tool_id": "run_code",
                                        "limit": 1,
                                    })
                                    return {
                                        "success": False,
                                        "error_code": "sandbox_execution_limit_reached",
                                        "error_message": "Validator sandbox execution limit reached; report existing evidence.",
                                    }
                                runtime_handle = cached_execution_handle()
                                if runtime_handle is None:
                                    return {
                                        "success": False,
                                        "error_code": "execution_environment_not_started",
                                        "error_message": "Start the execution environment before running validator checks.",
                                    }
                                sandbox_execution_attempts += 1
                                result = dict(await _maybe_await(
                                    agentbay_toolkit.run_code(runtime_handle, language, code, timeout_seconds)
                                ))
                                if result.get("success") is True:
                                    sandbox_execution_successes += 1
                                return result

                            run_code.__name__ = "run_code"
                            tool = run_code
                        else:
                            tool = getattr(agentbay_toolkit, tool_id)
                        tools.append(tool)
                        tool_names.add(tool_id)

            if any(tool_id in IMAGE_RUNTIME_TOOL_IDS + VIDEO_RUNTIME_TOOL_IDS for tool_id in granted_tool_ids):
                media_client = self._http_client_factory()

            if any(tool_id in IMAGE_RUNTIME_TOOL_IDS for tool_id in granted_tool_ids):
                image_toolkit = self._image_toolkit_factory(
                    settings=self._settings,
                    artifact_store=self._media_store,
                    http_client=media_client,
                    event_sink=toolkit_event_sink,
                )
                for tool_id in granted_tool_ids:
                    if tool_id in IMAGE_RUNTIME_TOOL_IDS and hasattr(image_toolkit, tool_id):
                        tool = getattr(image_toolkit, tool_id)
                        tools.append(tool)
                        tool_names.add(tool_id)

            if any(tool_id in VIDEO_RUNTIME_TOOL_IDS for tool_id in granted_tool_ids):
                video_toolkit = self._video_toolkit_factory(
                    settings=self._settings,
                    artifact_store=self._media_store,
                    http_client=media_client,
                    event_sink=toolkit_event_sink,
                )
                for tool_id in granted_tool_ids:
                    if tool_id in VIDEO_RUNTIME_TOOL_IDS and hasattr(video_toolkit, tool_id):
                        tool = getattr(video_toolkit, tool_id)
                        tools.append(tool)
                        tool_names.add(tool_id)

            if "execute_notes_demo" in granted_tool_ids and _execute_notes_demo_available(self._settings):
                tools.append(execute_notes_demo_tool)
                tool_names.add("execute_notes_demo")
            if "risk_assessment" in granted_tool_ids and _risk_assessment_available():
                tools.append(risk_assessment_tool)
                tool_names.add("risk_assessment")
            if "context7_lookup" in granted_tool_ids:
                raise NodeExecutionError("capability", "context7_runtime_unavailable", "Context7 execution is not wired into the default Phase 4 executor")
            if any(tool_id in LOCAL_ARTIFACT_TOOL_IDS for tool_id in granted_tool_ids):
                local_artifacts = LocalArtifactTools(
                    role_key=assignment.agent_template_id,
                    artifact_root=self._artifact_root,
                    event_sink=self._toolkit_event_sink(assignment, node, internal_tool_trace),
                )
                for tool_id in granted_tool_ids:
                    if tool_id in LOCAL_ARTIFACT_TOOL_IDS and hasattr(local_artifacts, tool_id):
                        tool = getattr(local_artifacts, tool_id)
                        tools.append(tool)
                        tool_names.add(tool_id)
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError("capability", "toolkit_construction_failed", str(exc)) from exc

        granted_callable_names = {_callable_tool_name(tool) for tool in tools}
        unexpected = sorted(granted_callable_names - set(granted_tool_ids))
        if unexpected:
            raise NodeExecutionError("capability", "toolkit_exposed_ungranted_tool", f"Toolkit exposed ungranted tools: {unexpected}")

        prompt = self._build_prompt(
            assignment=assignment,
            tool_ids=sorted(tool_names),
            skill_instructions=materialized_agent.skill_instructions,
            direct_dependency_outputs=direct_dependency_outputs,
            user_request=self._current_user_request,
        )
        session_id = f"composition-{assignment.id}-attempt-{attempt}-{uuid4().hex[:8]}"
        try:
            agent = self._build_agent_factory(
                materialized_agent.identity,
                self._settings,
                tools=tools,
                session_id=session_id,
                session_state={},
                extra_instructions=[
                    "You are executing one bounded assignment in a composed team runtime.",
                    "Use only the granted tools provided on this run.",
                    "Return only the work product and bounded artifact references needed by dependants.",
                ],
                tool_call_limit=_tool_call_limit_for_assignment(assignment, len(tools)),
            )
            if not hasattr(agent, "arun"):
                raise NodeExecutionError("capability", "agent_runtime_missing_arun", "Composition runtime agent must expose arun")
            response = await agent.arun(prompt)
            result = _normalize_agent_response(response)
            if (
                assignment.template_version is not None
                and assignment.expected_artifacts
                and "export_artifact" in granted_tool_ids
                and agentbay_toolkit is not None
            ):
                try:
                    result = await self._enforce_required_artifact_exports(
                        assignment=assignment,
                        node=node,
                        result=result,
                        internal_trace=internal_tool_trace,
                        agentbay_toolkit=agentbay_toolkit,
                        cached_start_result=cached_start_result,
                    )
                except NodeExecutionError as exc:
                    if exc.code != "required_artifact_export_failed":
                        raise
                    await _emit_event(
                        self._event_sink,
                        "composition_artifact_correction_requested",
                        {
                            "assignment_id": assignment.id,
                            "node_id": node.id,
                            "artifact_paths": list(assignment.expected_artifacts),
                        },
                    )
                    correction_prompt = (
                        "Your previous turn returned before completing the assignment. "
                        "Using the existing execution environment and only your granted tools, create or repair every "
                        "owned artifact listed below, then run the required real sandbox check and return strict JSON. "
                        "Do not merely describe work and do not claim a file exists unless write_text_file succeeded.\n"
                        f"The runtime's exact failed export was: {_bounded_text(str(exc), 1000)}\n"
                        f"Owned artifacts: {json.dumps(list(assignment.expected_artifacts), sort_keys=True)}"
                    )
                    correction_response = await agent.arun(correction_prompt)
                    correction_result = _normalize_agent_response(correction_response)
                    result = await self._enforce_required_artifact_exports(
                        assignment=assignment,
                        node=node,
                        result=correction_result,
                        internal_trace=internal_tool_trace,
                        agentbay_toolkit=agentbay_toolkit,
                        cached_start_result=cached_start_result,
                    )
                    await _emit_event(
                        self._event_sink,
                        "composition_artifact_correction_completed",
                        {
                            "assignment_id": assignment.id,
                            "node_id": node.id,
                            "artifact_paths": list(assignment.expected_artifacts),
                        },
                    )
            if agentbay_toolkit is not None:
                await self._enforce_required_repository_check(
                    assignment=assignment,
                    node=node,
                    internal_trace=internal_tool_trace,
                    agentbay_toolkit=agentbay_toolkit,
                    cached_start_result=cached_start_result,
                )
            if warnings:
                result["warnings"] = warnings
            failed_required = self._failed_required_tool_outcomes(assignment, granted_tool_ids, internal_tool_trace)
            if failed_required:
                raise NodeExecutionError(
                    "capability",
                    "mandatory_tool_reported_failure",
                    f"Mandatory tool reported failure: {sorted(set(failed_required))}",
                )
            missing_required = self._missing_required_tool_evidence(assignment, granted_tool_ids, internal_tool_trace)
            if missing_required:
                raise NodeExecutionError(
                    "capability",
                    "mandatory_tool_evidence_missing",
                    f"Mandatory tool evidence missing: {sorted(set(missing_required))}",
                )
            merged_result = _merge_event_backed_output(assignment, result, internal_tool_trace)
            verified_media_artifacts = self._verified_generated_media_artifacts(internal_tool_trace)
            media_artifacts_by_expected_path: dict[str, dict[str, Any]] = {}
            for expected_path, media_artifact in zip(
                (
                    path
                    for path in assignment.expected_artifacts
                    if Path(str(path)).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm"}
                ),
                verified_media_artifacts,
                strict=False,
            ):
                media_artifacts_by_expected_path[str(expected_path)] = media_artifact
            if media_artifacts_by_expected_path:
                # Preserve the produced-media reference for projections while
                # keeping workspace_exports exclusively for AgentBay files.
                merged_result["media_artifacts"] = [
                    {"expected_artifact": path, **artifact}
                    for path, artifact in media_artifacts_by_expected_path.items()
                ]
                merged_result["artifact_refs"] = _merge_unique_strings(
                    merged_result.get("artifact_refs") if isinstance(merged_result.get("artifact_refs"), list) else [],
                    [artifact["artifact_id"] for artifact in media_artifacts_by_expected_path.values()],
                    limit=_MAX_OUTPUT_ITEMS,
                )
            if assignment.agent_template_id == "test_engineer" and merged_result.get("passed") is not True:
                failures = merged_result.get("failures")
                failure_summary = failures if isinstance(failures, list) else []
                raise NodeExecutionError(
                    "validation",
                    "independent_validation_failed",
                    f"Independent validator did not pass the dependency artifacts: {failure_summary[:5]}",
                )
            if assignment.template_version is not None and assignment.expected_artifacts:
                exported_paths = {
                    str(item.get("workspace_relative_path") or item.get("path") or "")
                    .replace("\\", "/")
                    .removeprefix("/workspace/")
                    .lstrip("/")
                    for item in merged_result.get("workspace_exports", [])
                    if isinstance(item, Mapping)
                }
                missing_artifacts = sorted(
                    str(path).replace("\\", "/").removeprefix("/workspace/").lstrip("/")
                    for path in assignment.expected_artifacts
                    if (
                        str(path).replace("\\", "/").removeprefix("/workspace/").lstrip("/") not in exported_paths
                        and str(path) not in media_artifacts_by_expected_path
                    )
                )
                if missing_artifacts:
                    raise NodeExecutionError(
                        "validation",
                        "required_artifacts_missing",
                        f"Assignment did not export every owned artifact: {missing_artifacts}",
                    )
            return merged_result
        finally:
            if agentbay_toolkit is not None:
                try:
                    close_results = await _maybe_await(agentbay_toolkit.close_all())
                except Exception as exc:
                    await _emit_event(
                        self._event_sink,
                        "composition_assignment_cleanup_warning",
                        {
                            "assignment_id": assignment.id,
                            "node_id": node.id,
                            "warning": _bounded_text(exc),
                        },
                    )
                else:
                    cleanup_results = [
                        {
                            "success": bool(item.get("success", False)),
                            "request_id": item.get("request_id"),
                            "error_code": item.get("error_code"),
                            "error_message": item.get("error_message"),
                            "closed": bool((item.get("data") or {}).get("closed", False)) if isinstance(item.get("data"), Mapping) else False,
                            "idempotent": bool((item.get("data") or {}).get("idempotent", False)) if isinstance(item.get("data"), Mapping) else False,
                        }
                        for item in close_results[:_MAX_OUTPUT_ITEMS]
                    ]
                    cleanup_success = bool(cleanup_results) and all(
                        item["success"] and item["closed"] for item in cleanup_results
                    )
                    await _emit_event(
                        self._event_sink,
                        "composition_assignment_cleanup_completed",
                        {
                            "assignment_id": assignment.id,
                            "node_id": node.id,
                            "success": cleanup_success,
                            "results": cleanup_results,
                        },
                    )
                    failures = [
                        _bounded_text(item.get("error_message") or item.get("error_code") or "cleanup failed")
                        for item in close_results
                        if not item.get("success", False)
                        or not isinstance(item.get("data"), Mapping)
                        or not item["data"].get("closed", False)
                    ]
                    if failures:
                        warnings.extend(failures)
                        await _emit_event(
                            self._event_sink,
                            "composition_assignment_cleanup_warning",
                            {
                                "assignment_id": assignment.id,
                                "node_id": node.id,
                                "warnings": failures[:_MAX_OUTPUT_ITEMS],
                            },
                        )
            if media_client is not None:
                media_client.close()

    def _build_prompt(
        self,
        *,
        assignment: TeamAssignment,
        tool_ids: Sequence[str],
        skill_instructions: Sequence[str],
        direct_dependency_outputs: Mapping[str, Mapping[str, Any]],
        user_request: str,
    ) -> str:
        verified_skills = "\n\n".join(
            _bounded_text(content, _MAX_PROMPT_TEXT) for content in skill_instructions
        )
        repository_check = (
            "Before returning, call execute_command once with command_id `python_compile`, "
            "arguments {\"target\": \"/workspace/repo\"}, and a bounded timeout; a successful real execution event is mandatory. "
            "After it succeeds, do not call execute_command or run_code again for this repository assignment.\n"
            if any(str(path).replace("\\", "/").startswith("repo/") for path in assignment.expected_artifacts)
            else "A successful real sandbox check or required browser render is mandatory before returning.\n"
        )
        return (
            "Complete the assignment using only the granted tools.\n"
            "Return exactly one JSON object with no markdown or prose outside the JSON.\n"
            "Always include an `artifact_refs` array. Include only durable local artifact paths you directly observed from tool results.\n"
            "Create or update every expected artifact before optional exploration or repeated checks. "
            "The runtime verifies and finalizes durable exports after your response. If you export an artifact yourself, "
            "include a `workspace_exports` entry shaped as {\"workspace_relative_path\": str, \"artifact_ref\": str}.\n"
            "Use execution calls economically: inspect only relevant inputs, never repeat a failed call with the same arguments, "
            "and stop once every expected path exists and the required checks have run.\n"
            "The bounded user request already contains the public fixture manifest and source contents. Use that supplied "
            "evidence directly; do not spend tool calls rereading or listing inputs already present in the request.\n"
            "Preserve every original public callable, import path, and externally visible behavior unless the assignment "
            "explicitly authorizes an interface change. Repair existing entry points in place and verify them directly; a "
            "new helper does not substitute for the fixture's original interface.\n"
            "Every machine-readable deliverable must explicitly cover all artifact semantics stated near the beginning of "
            "the bounded user request. Required facts must be machine-readable, including negative or failure-retention "
            "evidence and real command evidence where requested; narrative prose alone is insufficient.\n"
            "For execute_command, the only valid command_id values are `pytest_target` and `python_compile`; "
            "each accepts exactly {\"target\": str}. Do not send shell commands as command_id values.\n"
            f"{repository_check}"
            "Use run_code only for bounded Python or JavaScript that complies with sandbox source policy; "
            "prefer the allowlisted commands for compilation or tests.\n"
            "For validator assignments, call report_independent_validation exactly once after checks complete. Its checks, "
            "failures, and inspected_artifact_refs parameters each accept either a JSON array of strings or a JSON object "
            "whose keys and values are strings; pass native JSON values, not JSON encoded inside strings. Also include those "
            "four fields plus `passed` in the final JSON. The tool also accepts `failed_checks` as an alias for `failures`.\n"
            "A validator must judge only the durable artifacts present in direct dependency outputs. Do not require or fail "
            "on runtime-owned cleanup markers or other files that are not among those dependency exports.\n"
            "A validator must exercise the fixture's original public entry points and reject interface removal or replacement. "
            "It must also reject machine-readable deliverables that omit any stated artifact semantic, bury required facts "
            "only in prose, or cite commands that were not executed.\n"
            "A validator may complete exactly one successful combined sandbox execution call, with no more than three bounded attempts. "
            "Combine all behavioral, report-schema, rollback, and adversarial assertions into one run_code call whenever "
            "possible, correct a rejected call once, then report the evidence without further exploration.\n"
            "For validator run_code, use pure Python built-ins and literal content already returned by inspect_artifact. Do not "
            "import modules, access the filesystem, invoke dynamic execution, or use restricted process/environment capabilities. "
            "Do not call pytest_target unless inspected fixture evidence proves a test target exists.\n"
            f"Assignment objective: {_bounded_text(assignment.objective, _MAX_PROMPT_TEXT)}\n"
            f"Bounded user request: {_bounded_text(user_request, _MAX_PROMPT_TEXT)}\n"
            f"Owned paths: {json.dumps(list(assignment.owned_paths)[:_MAX_OUTPUT_ITEMS])}\n"
            f"Expected artifacts: {json.dumps(list(assignment.expected_artifacts)[:_MAX_OUTPUT_ITEMS])}\n"
            f"Acceptance checks: {json.dumps(list(assignment.acceptance_checks)[:_MAX_OUTPUT_ITEMS])}\n"
            f"Dependency outputs: {json.dumps(_bounded_value(direct_dependency_outputs), sort_keys=True)}\n"
            f"Granted tools: {json.dumps(list(tool_ids))}\n"
            f"Verified repository skill instructions:\n{verified_skills}\n"
            "Do not claim unexecuted work. Do not use tools that were not granted. Return bounded structured results."
        )


class CompositionRuntime:
    """Credential-aware composition execution runtime with injectable collaborators."""

    def __init__(
        self,
        settings: Settings,
        artifact_root: str | Path,
        *,
        assignment_executor: AssignmentExecutor | None = None,
        event_sink: EventSink | None = None,
        preflight_fn: PreflightFn = model_capability_preflight,
        build_agent_factory: Callable[..., Any] = build_agno_agent,
        agentbay_toolkit_factory: Callable[..., Any] | None = None,
        image_toolkit_factory: Callable[..., Any] = ImageGenerationTools,
        video_toolkit_factory: Callable[..., Any] = VideoGenerationTools,
        media_store_factory: Callable[[Path], MediaArtifactStore] = MediaArtifactStore,
        http_client_factory: Callable[[], httpx.Client] = httpx.Client,
        clock: ClockFn | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._artifact_root = Path(artifact_root).resolve()
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._event_sink = event_sink or (lambda _event_type, _payload: None)
        self._preflight_fn = preflight_fn
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep
        self._invocation_hook: InvocationHook | None = None
        self._media_store = media_store_factory(self._artifact_root / "media")
        resolved_agentbay_toolkit_factory = agentbay_toolkit_factory or _lazy_agentbay_toolkit_factory
        self._assignment_executor = assignment_executor or AgnoAssignmentExecutor(
            settings=settings,
            artifact_root=self._artifact_root,
            availability_provider=self.available_tool_ids,
            event_sink=self._event_sink,
            build_agent_factory=build_agent_factory,
            agentbay_toolkit_factory=resolved_agentbay_toolkit_factory,
            image_toolkit_factory=image_toolkit_factory,
            video_toolkit_factory=video_toolkit_factory,
            media_store=self._media_store,
            http_client_factory=http_client_factory,
        )

    def set_invocation_hook(self, hook: InvocationHook | None) -> None:
        """Attach a pre-execution leader invocation check for fixed plans."""

        self._invocation_hook = hook

    def available_tool_ids(self) -> RuntimeAvailability:
        """Return the truthful canonical tool set for this runtime instance."""

        preflight = self._preflight_fn(self._settings)
        provider_services = dict(preflight.get("provider_services", {}))
        tool_ids: list[str] = []
        if provider_services.get("agentbay", {}).get("ready"):
            tool_ids.extend(tool_id for tool_id in AGENTBAY_RUNTIME_TOOL_IDS if tool_id != "browser_render")
        if provider_services.get("browser", {}).get("ready"):
            tool_ids.append("browser_render")
        if provider_services.get("image", {}).get("ready"):
            tool_ids.extend(IMAGE_RUNTIME_TOOL_IDS)
        if provider_services.get("video", {}).get("ready"):
            tool_ids.extend(VIDEO_RUNTIME_TOOL_IDS)
        if _context7_available(self._settings):
            tool_ids.append("context7_lookup")
        if _execute_notes_demo_available(self._settings):
            tool_ids.append("execute_notes_demo")
        if _risk_assessment_available():
            tool_ids.append("risk_assessment")
        tool_ids.extend(LOCAL_ARTIFACT_TOOL_IDS)
        blockers = [AvailabilityBlocker.model_validate(item) for item in preflight.get("typed_blockers", [])]
        return RuntimeAvailability(tool_ids=sorted(set(canonical_tool_id(tool_id) for tool_id in tool_ids)), blockers=blockers)

    def build_composition_context(
        self,
        task_id: str,
        user_request: str,
        acceptance_requirements: Sequence[str],
        *,
        required_capabilities: Sequence[str] = (),
        unresolved_user_requirements: Sequence[str] = (),
    ) -> CompositionContext:
        """Build a truthful composition context from current settings and preflight."""

        availability = self.available_tool_ids()
        return CompositionContext(
            task_id=task_id,
            task_summary=_bounded_text(user_request, _MAX_PROMPT_TEXT),
            user_request=_bounded_text(user_request, _MAX_PROMPT_TEXT),
            required_capabilities=list(required_capabilities),
            available_tool_ids=list(availability.tool_ids),
            acceptance_requirements=list(acceptance_requirements),
            unresolved_user_requirements=list(unresolved_user_requirements),
            limits=limits_from_settings(self._settings),
        )

    async def execute_plan(
        self,
        plan: TeamCompositionPlan,
        task_id: str,
        user_request: str,
        *,
        session_state: dict[str, Any] | None = None,
        global_cancellation_event: asyncio.Event | None = None,
    ) -> CompositionExecutionResult:
        """Execute a validated plan with isolated assignment identities."""

        del task_id, session_state
        if hasattr(self._assignment_executor, "set_user_request"):
            self._assignment_executor.set_user_request(user_request)
        materialized_by_assignment: dict[str, MaterializedAssignmentAgent] = {}
        node_outputs: dict[str, dict[str, Any]] = {}
        for assignment in plan.assignments:
            materialized = _build_assignment_identity(assignment)
            materialized_by_assignment[assignment.id] = materialized
            await _emit_event(
                self._event_sink,
                "composition_assignment_materialized",
                materialized.summary.model_dump(mode="json"),
            )

        async def node_runner(
            node: WorkNode,
            assignment: TeamAssignment,
            attempt: int,
            cancellation_event: asyncio.Event,
        ) -> Mapping[str, Any] | None:
            if assignment.template_version is not None:
                if self._invocation_hook is not None:
                    hook_result = self._invocation_hook(node, assignment)
                    if inspect.isawaitable(hook_result):
                        await hook_result
                await _emit_event(
                    self._event_sink,
                    "specialist_invocation_started",
                    {
                        "assignment_id": assignment.id,
                        "node_id": node.id,
                        "template_id": assignment.agent_template_id,
                        "template_version": assignment.template_version,
                        "depends_on": list(node.depends_on),
                    },
                )
            granted_tool_ids = _canonical_granted_tool_ids(assignment)
            unavailable = sorted(set(granted_tool_ids) - set(self.available_tool_ids().tool_ids))
            if unavailable:
                raise NodeExecutionError(
                    "capability",
                    "unavailable_tool_grant",
                    f"Granted tools are not available in this runtime: {unavailable}",
                )
            result = await self._assignment_executor(
                node=node,
                assignment=assignment,
                materialized_agent=materialized_by_assignment[assignment.id],
                direct_dependency_outputs=_direct_dependency_outputs(node, node_outputs),
                attempt=attempt,
                cancellation_event=cancellation_event,
            )
            public = _public_output(result)
            node_outputs[node.id] = public
            return public

        graph_result = await BoundedWorkGraphExecutor(
            plan,
            limits_from_settings(self._settings),
            node_runner,
            self._event_sink,
            sleep=self._sleep,
            clock=self._clock,
            retry_settings=RetrySettings(
                max_attempts=self._settings.subtask_max_attempts,
                backoff_base_seconds=self._settings.subtask_backoff_base_seconds,
                backoff_cap_seconds=self._settings.subtask_backoff_cap_seconds,
            ),
            global_cancellation_event=global_cancellation_event,
        ).run()

        return CompositionExecutionResult(
            graph_result=graph_result,
            materialized_agents=[agent.summary for agent in materialized_by_assignment.values()],
            node_outputs={node_id: _public_output(record.result) for node_id, record in graph_result.nodes.items()},
            node_artifact_refs={node_id: list(record.artifact_refs) for node_id, record in graph_result.nodes.items()},
            availability_blockers=self.available_tool_ids().blockers,
        )


async def _emit_event(event_sink: EventSink, event_type: str, payload: Mapping[str, Any]) -> None:
    result = event_sink(event_type, dict(payload))
    if inspect.isawaitable(result):
        await result


async def _maybe_await(value: Any) -> Any:
    """Await async adapter wrappers while preserving synchronous toolkits."""

    return await value if inspect.isawaitable(value) else value


def _lazy_agentbay_toolkit_factory(*args: Any, **kwargs: Any) -> Any:
    from society.tools.agentbay import AgentBayTools

    return AgentBayTools(*args, **kwargs)
