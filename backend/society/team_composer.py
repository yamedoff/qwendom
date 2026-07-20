"""Typed team composition and one-shot recomposition control.

This module keeps provider-backed planning separate from static validation.
Providers may propose a ``TeamCompositionPlan``, but the plan is always
validated locally before any later executor or provider-backed tool runs.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agno.agent import Agent
from config import Settings
from society.capability_registry import (
    canonical_tool_id,
    get_role_capabilities,
    list_specialist_templates,
)
from society.schemas.team_composition import (
    CompositionLimits,
    PlanValidationIssue,
    TeamCompositionPlan,
    TeamCompositionValidationError,
    TeamCompositionEventType,
    validate_team_composition_plan,
)

TEAM_COMPOSITION_BLOCKED = "team_composition_blocked"

_CORE_ROLE_IDS: tuple[str, ...] = (
    "coordinator",
    "architect",
    "researcher",
    "builder",
    "critic",
)
_CAPABILITY_FAILURE_CODES: frozenset[str] = frozenset({
    "unowned_required_capability",
    "unknown_agent_template",
    "unavailable_agent_template",
    "assignment_capability_mismatch",
    "grant_capability_mismatch",
    "forbidden_tool_grant",
    "unavailable_tool_grant",
    "missing_required_tool",
})
_USER_REQUIREMENT_CODES: frozenset[str] = frozenset({
    "missing_acceptance_checks",
    "missing_required_validation_check",
    "artifact_without_independent_validation",
})

EventSink = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]


class CatalogRoleSnapshot(BaseModel):
    """Public, secret-safe registry facts exposed to the planning model."""

    agent_template_id: str
    capabilities: list[str] = Field(default_factory=list)
    allowed_tool_ids: list[str] = Field(default_factory=list)
    required_tool_ids: list[str] = Field(default_factory=list)
    can_write_product_files: bool = False
    can_accept_artifacts: bool = False
    network_policy: str = "disabled"


class CompositionContext(BaseModel):
    """Private composition input for a single task.

    ``task_summary`` and ``user_request`` may include sensitive task details.
    They are passed to the provider, but local events emitted by this module
    must never include them.
    """

    task_id: str
    task_summary: str
    user_request: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    available_tool_ids: list[str] | None = None
    available_agent_template_ids: list[str] | None = None
    acceptance_requirements: list[str] = Field(default_factory=list)
    unresolved_user_requirements: list[str] = Field(default_factory=list)
    limits: CompositionLimits = Field(default_factory=CompositionLimits)
    attempt_ceiling: int = 2


class TeamCompositionResult(BaseModel):
    """Successful validated composition outcome."""

    plan: TeamCompositionPlan
    attempt_count: int
    recomposed: bool
    validation_issue_history: list[list[PlanValidationIssue]] = Field(default_factory=list)


class TeamCompositionBlocked(RuntimeError):
    """Raised when composition cannot proceed safely without pausing."""

    def __init__(
        self,
        *,
        category: str,
        message: str,
        issues: Sequence[PlanValidationIssue],
        attempt_count: int = 1,
        validation_issue_history: Sequence[Sequence[PlanValidationIssue]] = (),
        pause_for_user: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.attempt_count = max(1, int(attempt_count))
        self.pause_for_user = pause_for_user
        self.issues = [issue.model_copy(deep=True) for issue in issues]
        self.validation_issue_history = [
            [issue.model_copy(deep=True) for issue in issue_group]
            for issue_group in validation_issue_history
        ]


class TeamCompositionProviderError(RuntimeError):
    """Typed provider failure that should not trigger recomposition."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TeamPlanProvider(Protocol):
    """Provider protocol for one-shot plan proposal and one-shot recomposition."""

    async def propose(
        self,
        context: CompositionContext,
        catalog_snapshot: Sequence[CatalogRoleSnapshot],
        previous_issue_codes: Sequence[str],
        attempt_number: int,
    ) -> TeamCompositionPlan:
        """Return a typed plan or raise a typed provider error."""


class AgnoTeamPlanProvider:
    """Agno-backed planner that requests the smallest valid capable team."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        if model is None and agent_factory is None:
            raise ValueError("AgnoTeamPlanProvider requires a model or agent_factory")
        self._model = model
        self._agent_factory = agent_factory or Agent

    async def propose(
        self,
        context: CompositionContext,
        catalog_snapshot: Sequence[CatalogRoleSnapshot],
        previous_issue_codes: Sequence[str],
        attempt_number: int,
    ) -> TeamCompositionPlan:
        """Request a structured plan from a coordinator-style Agno agent."""

        agent = self._agent_factory(
            name="Coordinator",
            role="Team composition coordinator",
            model=self._model,
            instructions=self._build_instructions(context, catalog_snapshot, previous_issue_codes, attempt_number),
            output_schema=TeamCompositionPlan,
            structured_outputs=True,
            markdown=True,
        )
        prompt = self._build_prompt(context, previous_issue_codes, attempt_number)
        response = await _call_agent(agent, prompt)
        try:
            return self._parse_response(response)
        except Exception as exc:  # pragma: no cover - exact branches are covered.
            if isinstance(exc, TeamCompositionProviderError):
                raise
            raise TeamCompositionProviderError(
                "provider_malformed_response",
                f"team plan provider returned malformed output: {exc}",
            ) from exc

    def _build_instructions(
        self,
        context: CompositionContext,
        catalog_snapshot: Sequence[CatalogRoleSnapshot],
        previous_issue_codes: Sequence[str],
        attempt_number: int,
    ) -> list[str]:
        catalog_json = json.dumps(
            [entry.model_dump(mode="json") for entry in catalog_snapshot],
            indent=2,
            sort_keys=True,
        )
        issue_text = ", ".join(previous_issue_codes) if previous_issue_codes else "none"
        return [
            "You are the typed team composition coordinator.",
            "Use only the accepted catalog below. Do not invent roles, capabilities, tools, or hidden fallbacks.",
            "Capability and tool policy: each assignment must request only capabilities that the selected role actually has, and every granted tool ID must come from that role's allowlist.",
            (
                "Owner limits: no minimum team size; choose the smallest capable team. "
                f"At most {context.limits.max_dynamic_specialists} dynamic specialists plus the coordinator."
            ),
            "Independent validation rule: the creator of an artifact must not be the decisive validator of that artifact.",
            "Work-graph dependency and conflict rule: model only real dependencies, and do not place conflicting writers or overlapping conflict domains in parallel-ready work.",
            (
                "If this is a recomposition attempt, fix only the prior typed issues and keep the team bounded. "
                f"Previous issue codes: {issue_text}."
            ),
            "Return a TeamCompositionPlan and omit no required capability unless it truly cannot be satisfied.",
            f"Accepted catalog:\n{catalog_json}",
            f"Attempt number: {attempt_number} of {context.attempt_ceiling}.",
        ]

    def _build_prompt(
        self,
        context: CompositionContext,
        previous_issue_codes: Sequence[str],
        attempt_number: int,
    ) -> str:
        issue_text = ", ".join(previous_issue_codes) if previous_issue_codes else "none"
        available_tools = (
            sorted({canonical_tool_id(tool_id) for tool_id in context.available_tool_ids})
            if context.available_tool_ids is not None
            else None
        )
        return (
            "Create the smallest capable team composition plan.\n"
            f"Task ID: {context.task_id}\n"
            f"Attempt: {attempt_number}/{context.attempt_ceiling}\n"
            f"Private task summary: {context.task_summary}\n"
            f"Private user request: {context.user_request}\n"
            f"Required capabilities: {json.dumps(sorted(set(context.required_capabilities)))}\n"
            f"Available runtime tool IDs: {json.dumps(available_tools)}\n"
            f"Acceptance requirements: {json.dumps(context.acceptance_requirements)}\n"
            f"Previous issue codes: {json.dumps(list(previous_issue_codes))}\n"
            "No minimum team size. Prefer the smallest capable team.\n"
            "Do not add roles that do not materially improve capability coverage or independent validation."
        )

    def _parse_response(self, response: Any) -> TeamCompositionPlan:
        if response is None:
            raise TeamCompositionProviderError("provider_empty_response", "team plan provider returned no response")
        return _coerce_team_plan(response)


class TeamComposer:
    """Compose a validated team plan with at most one bounded recomposition."""

    def __init__(
        self,
        provider: TeamPlanProvider,
        event_sink: EventSink | None = None,
        *,
        registry: Mapping[str, Any] | None = None,
    ) -> None:
        self._provider = provider
        self._event_sink = event_sink or (lambda _event_type, _payload: None)
        self._registry = dict(registry) if registry is not None else _build_registry_mapping()

    async def compose(self, context: CompositionContext) -> TeamCompositionResult:
        """Request, validate, optionally recompose once, and then return or block."""

        catalog_snapshot = build_catalog_snapshot()
        if context.available_agent_template_ids is not None:
            allowed_template_ids = set(context.available_agent_template_ids)
            catalog_snapshot = [
                snapshot
                for snapshot in catalog_snapshot
                if snapshot.agent_template_id in allowed_template_ids
            ]
        validation_issue_history: list[list[PlanValidationIssue]] = []

        try:
            initial_plan = await self._provider.propose(context, catalog_snapshot, [], 1)
        except TeamCompositionProviderError as exc:
            issues = [PlanValidationIssue(code=exc.code, message=str(exc))]
            await self._emit_blocked_event(context.task_id, 1, issues)
            raise TeamCompositionBlocked(
                category="missing_system_capability",
                message=str(exc),
                issues=issues,
                attempt_count=1,
                validation_issue_history=validation_issue_history,
            ) from exc

        await self._emit_proposed_event(context.task_id, 1, initial_plan)
        first_error = self._validate(initial_plan, context)
        if first_error is None:
            await self._emit_validated_event(context.task_id, 1, initial_plan)
            return TeamCompositionResult(plan=initial_plan, attempt_count=1, recomposed=False)

        validation_issue_history.append([issue.model_copy(deep=True) for issue in first_error.issues])
        await self._emit_recomposition_requested_event(context.task_id, 1, first_error.issues)

        if context.attempt_ceiling < 2:
            await self._emit_blocked_event(context.task_id, 1, first_error.issues)
            raise TeamCompositionBlocked(
                category=_classify_blocked_category(context, first_error.issues),
                message="team composition failed validation",
                issues=first_error.issues,
                attempt_count=1,
                validation_issue_history=validation_issue_history,
            )

        try:
            second_plan = await self._provider.propose(
                context,
                catalog_snapshot,
                [issue.code for issue in first_error.issues],
                2,
            )
        except TeamCompositionProviderError as exc:
            issues = [PlanValidationIssue(code=exc.code, message=str(exc))]
            await self._emit_blocked_event(context.task_id, 2, issues)
            raise TeamCompositionBlocked(
                category="missing_system_capability",
                message=str(exc),
                issues=issues,
                attempt_count=2,
                validation_issue_history=validation_issue_history,
            ) from exc

        await self._emit_proposed_event(context.task_id, 2, second_plan)
        second_error = self._validate(second_plan, context)
        if second_error is None:
            await self._emit_validated_event(context.task_id, 2, second_plan)
            return TeamCompositionResult(
                plan=second_plan,
                attempt_count=2,
                recomposed=True,
                validation_issue_history=validation_issue_history,
            )

        validation_issue_history.append([issue.model_copy(deep=True) for issue in second_error.issues])
        await self._emit_blocked_event(context.task_id, 2, second_error.issues)
        raise TeamCompositionBlocked(
            category=_classify_blocked_category(context, second_error.issues),
            message="team composition remained invalid after recomposition",
            issues=second_error.issues,
            attempt_count=2,
            validation_issue_history=validation_issue_history,
        )

    def _validate(
        self,
        plan: TeamCompositionPlan,
        context: CompositionContext,
    ) -> TeamCompositionValidationError | None:
        try:
            validate_team_composition_plan(
                plan,
                self._registry,
                required_capabilities=context.required_capabilities,
                available_tool_ids=context.available_tool_ids,
                available_agent_template_ids=context.available_agent_template_ids,
                limits=context.limits,
            )
        except TeamCompositionValidationError as exc:
            return exc
        return None

    async def _emit_proposed_event(
        self,
        task_id: str,
        attempt: int,
        plan: TeamCompositionPlan,
    ) -> None:
        payload = _bounded_plan_event_payload(task_id, attempt, plan)
        await _emit_event(self._event_sink, TeamCompositionEventType.PROPOSED.value, payload)

    async def _emit_validated_event(
        self,
        task_id: str,
        attempt: int,
        plan: TeamCompositionPlan,
    ) -> None:
        payload = _bounded_plan_event_payload(task_id, attempt, plan)
        await _emit_event(self._event_sink, TeamCompositionEventType.VALIDATED.value, payload)

    async def _emit_recomposition_requested_event(
        self,
        task_id: str,
        attempt: int,
        issues: Sequence[PlanValidationIssue],
    ) -> None:
        await _emit_event(
            self._event_sink,
            TeamCompositionEventType.RECOMPOSITION_REQUESTED.value,
            {
                "task_id": task_id,
                "attempt": attempt,
                "issue_codes": [issue.code for issue in issues],
            },
        )

    async def _emit_blocked_event(
        self,
        task_id: str,
        attempt: int,
        issues: Sequence[PlanValidationIssue],
    ) -> None:
        await _emit_event(
            self._event_sink,
            TEAM_COMPOSITION_BLOCKED,
            {
                "task_id": task_id,
                "attempt": attempt,
                "issue_codes": [issue.code for issue in issues],
                "pause_for_user": True,
            },
        )


def build_catalog_snapshot() -> list[CatalogRoleSnapshot]:
    """Return the canonical accepted role catalog without mutating the registry."""

    snapshots: dict[str, CatalogRoleSnapshot] = {}
    specialist_templates = list_specialist_templates()
    for role_id in _CORE_ROLE_IDS:
        registration = get_role_capabilities(role_id)
        if registration is None:
            continue
        snapshots[role_id] = _snapshot_from_registration(role_id, registration)
    for role_id in sorted(specialist_templates):
        registration = get_role_capabilities(role_id)
        if registration is None:
            continue
        snapshots[role_id] = _snapshot_from_registration(role_id, registration)
    return [snapshots[role_id] for role_id in sorted(snapshots)]


def limits_from_settings(settings: Settings) -> CompositionLimits:
    """Project runtime settings into static composition limits."""

    return CompositionLimits(
        max_model_workers=settings.society_max_model_workers,
        max_agentbay_sessions=settings.society_max_agentbay_sessions,
        max_media_jobs=settings.society_max_media_jobs,
        max_dynamic_specialists=settings.society_max_dynamic_specialists,
    )


def _snapshot_from_registration(role_id: str, registration: Any) -> CatalogRoleSnapshot:
    return CatalogRoleSnapshot(
        agent_template_id=role_id,
        capabilities=_stable_distinct(registration.capabilities),
        allowed_tool_ids=_stable_distinct(canonical_tool_id(tool_id) for tool_id in registration.allowed_tools),
        required_tool_ids=_stable_distinct(canonical_tool_id(tool_id) for tool_id in registration.required_tools),
        can_write_product_files=bool(registration.can_write_product_files),
        can_accept_artifacts=bool(registration.can_accept_artifacts),
        network_policy=str(registration.network_policy),
    )


def _build_registry_mapping() -> dict[str, Any]:
    registry: dict[str, Any] = {}
    for snapshot in build_catalog_snapshot():
        registration = get_role_capabilities(snapshot.agent_template_id)
        if registration is not None:
            registry[snapshot.agent_template_id] = registration
    return registry


def _stable_distinct(values: Sequence[str] | Any) -> list[str]:
    items = list(values)
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _bounded_plan_event_payload(
    task_id: str,
    attempt: int,
    plan: TeamCompositionPlan,
) -> dict[str, Any]:
    template_ids = sorted({assignment.agent_template_id for assignment in plan.assignments})
    return {
        "task_id": task_id,
        "attempt": attempt,
        "assignment_template_ids": template_ids,
        "assignment_count": len(plan.assignments),
        "work_node_count": len(plan.work_graph),
        "rationale_hash": hashlib.sha256(plan.selection_rationale.encode("utf-8")).hexdigest(),
    }


def _classify_blocked_category(
    context: CompositionContext,
    issues: Sequence[PlanValidationIssue],
) -> str:
    issue_codes = {issue.code for issue in issues}
    if issue_codes.intersection(_CAPABILITY_FAILURE_CODES):
        return "missing_system_capability"
    if context.unresolved_user_requirements and issue_codes and issue_codes.issubset(_USER_REQUIREMENT_CODES):
        return "missing_user_input"
    return "missing_system_capability"


async def _emit_event(event_sink: EventSink, event_type: str, payload: Mapping[str, Any]) -> None:
    result = event_sink(event_type, dict(payload))
    if inspect.isawaitable(result):
        await result


async def _call_agent(agent: Any, prompt: str) -> Any:
    if hasattr(agent, "arun"):
        result = agent.arun(prompt)
        if inspect.isawaitable(result):
            return await result
        return result
    if hasattr(agent, "run"):
        result = agent.run(prompt)
        if inspect.isawaitable(result):
            return await result
        return result
    raise TeamCompositionProviderError("provider_runtime_error", "team plan provider agent has no run method")


def _coerce_team_plan(value: Any) -> TeamCompositionPlan:
    if isinstance(value, TeamCompositionPlan):
        return value
    if isinstance(value, BaseModel):
        return TeamCompositionPlan.model_validate(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return TeamCompositionPlan.model_validate(dict(value))

    content = getattr(value, "content", None)
    if content is not None:
        return _coerce_team_plan(content)

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TeamCompositionProviderError(
                "provider_malformed_response",
                f"team plan provider did not return valid JSON: {exc}",
            ) from exc
        return TeamCompositionPlan.model_validate(decoded)

    raise TeamCompositionProviderError(
        "provider_malformed_response",
        f"unsupported team plan payload type: {type(value).__name__}",
    )
