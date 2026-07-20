"""Leader-facing fixed-specialist selection and replay-safe plan resolution.

The leader is allowed to choose template IDs, objectives, dependencies,
artifacts, and acceptance requirements. Tool grants, skill references, resource
policy, and sandbox policy never appear in leader-controlled schemas; they are
resolved exclusively from :mod:`society.capability_registry`.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from agno.agent import Agent

from .capability_registry import (
    CORE_ROLE_CAPABILITIES,
    ResolvedSpecialistBundle,
    SpecialistTemplate,
    get_fixed_specialist_template,
    list_fixed_specialist_templates,
    resolve_specialist_bundle,
)

_ARTIFACT_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
_IMAGE_ARTIFACT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_VIDEO_ARTIFACT_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv"})
_IMAGE_GENERATION_TOOL_IDS = frozenset({"generate_images", "publish_image"})
_VIDEO_GENERATION_TOOL_IDS = frozenset({"submit_text_to_video", "collect_video"})
from .schemas.team_composition import (
    TeamCompositionValidationError,
    ResolvedSkillBinding,
    TeamAssignment,
    TeamCompositionPlan,
    ToolGrant,
    WorkNode,
    validate_team_composition_plan,
)

EventSink = Callable[[str, Mapping[str, Any]], Any]


class ListSpecialistsCall(BaseModel):
    """Argument schema for the leader's catalog query."""

    model_config = ConfigDict(extra="forbid")


class SpecialistAssignmentSelection(BaseModel):
    """The only assignment fields a leader may control."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1, max_length=100)
    template_id: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(
        description="Exact assignment_ids that must complete before this assignment; provide [] when none.",
    )
    owned_artifacts: list[str] = Field(
        description="Exact workspace-relative deliverable paths this producer owns; provide [] for validators.",
    )
    acceptance_requirements: list[str] = Field(
        description="Concrete checks this assignment must satisfy; provide [] only when no acceptance applies.",
    )


class SelectSpecialistsCall(BaseModel):
    """Schema for a complete leader-selected team and work graph."""

    model_config = ConfigDict(extra="forbid")

    assignments: list[SpecialistAssignmentSelection] = Field(min_length=1, max_length=5)
    selection_rationale: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_graph_and_ownership(self) -> "SelectSpecialistsCall":
        """Reject ambiguous IDs, dependencies, cycles, and artifact ownership."""

        assignment_ids = [item.assignment_id for item in self.assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("duplicate_assignment_id")
        known = set(assignment_ids)
        owner_by_artifact: dict[str, str] = {}
        dependencies: dict[str, set[str]] = {}
        for item in self.assignments:
            unknown = set(item.depends_on) - known
            if unknown:
                raise ValueError(f"unknown_dependency:{item.assignment_id}:{sorted(unknown)}")
            if item.assignment_id in item.depends_on:
                raise ValueError(f"self_dependency:{item.assignment_id}")
            dependencies[item.assignment_id] = set(item.depends_on)
            for artifact in item.owned_artifacts:
                normalized = artifact.strip().replace("\\", "/")
                if not normalized:
                    raise ValueError(f"empty_artifact_path:{item.assignment_id}")
                previous = owner_by_artifact.get(normalized)
                if previous is not None:
                    raise ValueError(f"conflicting_artifact_ownership:{normalized}:{previous}:{item.assignment_id}")
                owner_by_artifact[normalized] = item.assignment_id

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(assignment_id: str) -> None:
            if assignment_id in visiting:
                raise ValueError(f"cyclic_dependency:{assignment_id}")
            if assignment_id in visited:
                return
            visiting.add(assignment_id)
            for dependency_id in dependencies[assignment_id]:
                visit(dependency_id)
            visiting.remove(assignment_id)
            visited.add(assignment_id)

        for assignment_id in assignment_ids:
            visit(assignment_id)
        return self


class InvokeSpecialistCall(BaseModel):
    """Argument schema for invoking one already-approved assignment."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1, max_length=100)


class SpecialistAvailability(BaseModel):
    """Public catalog entry including truthful runtime availability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    version: str
    role: str
    description: str
    capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...]
    skills: tuple[dict[str, str], ...]
    available: bool
    blocker_code: str | None = None
    blocker_message: str | None = None


class ListSpecialistsResult(BaseModel):
    """Validated result returned by the leader's catalog tool call."""

    model_config = ConfigDict(extra="forbid")

    specialists: list[SpecialistAvailability]


class ResolvedAssignmentMetadata(BaseModel):
    """Persistable replay metadata attached to one leader assignment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assignment_id: str
    template_id: str
    template_version: str
    objective: str
    capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...]
    tool_bundle_hash: str
    skill_ids: tuple[str, ...]
    skill_versions: tuple[str, ...]
    skill_hashes: tuple[str, ...]
    depends_on: tuple[str, ...]
    owned_artifacts: tuple[str, ...]
    acceptance_requirements: tuple[str, ...]
    validates_assignment_ids: tuple[str, ...]


class ResolvedSpecialistSelection(BaseModel):
    """Validated plan plus immutable metadata required for exact replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: TeamCompositionPlan
    assignments: tuple[ResolvedAssignmentMetadata, ...]


class SpecialistSelectionBlocker(BaseModel):
    """Typed, human-readable selection blocker emitted before external work."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    assignment_id: str | None = None
    template_id: str | None = None


class SpecialistSelectionError(ValueError):
    """Raised when fixed specialist resolution must fail closed."""

    def __init__(self, blockers: Sequence[SpecialistSelectionBlocker]) -> None:
        self.blockers = tuple(blockers)
        super().__init__("; ".join(blocker.message for blocker in blockers))


class FixedSpecialistProviderError(RuntimeError):
    """Typed failure from the elected-leader selection model call."""


class AgnoFixedSpecialistSelectionProvider:
    """Ask one leader-model identity for only the safe selection schema."""

    def __init__(
        self,
        *,
        model: Any | None = None,
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        if model is None and agent_factory is None:
            raise ValueError("A model or agent_factory is required for fixed specialist selection.")
        self._model = model
        self._agent_factory = agent_factory or Agent

    async def propose(
        self,
        *,
        task_summary: str,
        user_request: str,
        acceptance_requirements: Sequence[str],
        catalog: Sequence[SpecialistAvailability],
        required_artifacts: Sequence[str] = (),
        prior_blockers: Sequence[Mapping[str, Any]] = (),
        attempt_number: int = 1,
    ) -> SelectSpecialistsCall:
        """Return one schema-constrained leader selection without policy fields."""

        public_catalog = [item.model_dump(mode="json") for item in catalog]
        instructions = [
            "You are the elected leader of a temporary agent society.",
            "Choose who works from the fixed repository catalog; never choose or emit tools, skills, credentials, locks, providers, or sandbox policy.",
            "Use the smallest capable team. Every artifact requiring acceptance must have a separate dependent validation assignment.",
            "The union of producer owned_artifacts must contain every required artifact exactly once. "
            "Copy the paths verbatim; do not leave ownership implicit and do not add paths that are absent from Required artifact ownership.",
            "Every depends_on entry must copy one assignment_id from the same response exactly, character for character.",
            "A test_engineer is validation-only: its owned_artifacts must be empty, it must depend on every producer "
            "it validates, and it must have non-empty acceptance_requirements.",
            "Acceptance requirements may reference only paths present in Required artifact ownership. Runtime-owned cleanup "
            "markers or other unlisted paths must not be assigned to a specialist validator.",
            "Prefer one capable producer that owns every required artifact plus one dependent test_engineer. "
            "Do not create separate producer assignments merely for planning, testing, review, or reporting. "
            "Use multiple producers only when distinct required capabilities cannot be covered by one catalog template.",
            "Only use catalog entries whose available field is true.",
            "Do not assign generated image or video artifact paths to a template unless its catalog capabilities include the required media tools. "
            "Browser screenshots under screenshots/ are browser-render evidence, not generated media. When no eligible media template is available, omit media artifacts and record a missing-system-capability blocker; never simulate placeholder attempts.",
            "Return only the SelectSpecialistsCall structured object.",
            f"Fixed catalog: {json.dumps(public_catalog, sort_keys=True)}",
        ]
        agent = self._agent_factory(
            name="Elected Society Leader",
            role="Fixed specialist selector",
            model=self._model,
            instructions=instructions,
            output_schema=SelectSpecialistsCall,
            structured_outputs=False,
            use_json_mode=True,
            markdown=False,
        )
        prompt = (
            f"Attempt: {attempt_number}/2\n"
            f"Task summary: {task_summary}\n"
            f"User request: {user_request}\n"
            f"Acceptance requirements: {json.dumps(list(acceptance_requirements))}\n"
            f"Required artifact ownership: {json.dumps(list(required_artifacts))}\n"
            "Before returning, verify that every Required artifact ownership path appears in exactly one "
            "producer assignment's owned_artifacts.\n"
            f"Prior typed blockers to correct: {json.dumps(list(prior_blockers), sort_keys=True)}"
        )
        try:
            response = await agent.arun(prompt)
        except Exception as exc:
            raise FixedSpecialistProviderError(f"fixed specialist provider call failed: {exc}") from exc
        content = getattr(response, "content", response)
        try:
            if isinstance(content, SelectSpecialistsCall):
                return content
            if isinstance(content, BaseModel):
                return SelectSpecialistsCall.model_validate(content.model_dump(mode="json"))
            if isinstance(content, Mapping):
                return SelectSpecialistsCall.model_validate(dict(content))
            if isinstance(content, str):
                return SelectSpecialistsCall.model_validate_json(content)
        except Exception as exc:
            raise FixedSpecialistProviderError(f"fixed specialist provider returned malformed output: {exc}") from exc
        raise FixedSpecialistProviderError("fixed specialist provider returned no structured selection.")


class FixedSpecialistCoordinator:
    """Resolve leader choices into immutable bundles without external calls."""

    def __init__(self, available_tool_ids: Sequence[str], event_sink: EventSink | None = None) -> None:
        self._available_tool_ids = frozenset(available_tool_ids)
        self._event_sink = event_sink or (lambda _event_type, _payload: None)
        self._selected: ResolvedSpecialistSelection | None = None
        self._invoked_assignment_ids: set[str] = set()

    async def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        result = self._event_sink(event_type, dict(payload))
        if inspect.isawaitable(result):
            await result

    def list_specialists(self, _call: ListSpecialistsCall | None = None) -> list[SpecialistAvailability]:
        """Return fixed catalog entries and exact missing-tool blockers."""

        entries: list[SpecialistAvailability] = []
        for template in list_fixed_specialist_templates().values():
            missing = sorted(set(template.tool_ids) - self._available_tool_ids)
            entries.append(
                SpecialistAvailability(
                    template_id=template.template_id,
                    version=template.version,
                    role=template.role,
                    description=template.description,
                    capabilities=template.capabilities,
                    tool_ids=template.tool_ids,
                    skills=tuple(
                        {"skill_id": skill.skill_id, "version": skill.version, "sha256": skill.sha256}
                        for skill in template.skills
                    ),
                    available=not missing,
                    blocker_code="specialist_tools_unavailable" if missing else None,
                    blocker_message=f"Required tools are unavailable: {missing}" if missing else None,
                )
            )
        return entries

    async def select_specialists(
        self,
        call: SelectSpecialistsCall,
        *,
        task_summary: str,
        required_capabilities: Sequence[str] = (),
        required_artifacts: Sequence[str] = (),
        required_acceptance_requirements: Sequence[str] = (),
    ) -> ResolvedSpecialistSelection:
        """Resolve a leader selection, emitting either rejection or acceptance."""

        await self._emit(
            "specialist_selection_proposed",
            {
                "selection_rationale": call.selection_rationale,
                "assignments": [assignment.model_dump(mode="json") for assignment in call.assignments],
            },
        )
        try:
            resolved = self._resolve(
                call,
                task_summary=task_summary,
                required_capabilities=required_capabilities,
                required_artifacts=required_artifacts,
                required_acceptance_requirements=required_acceptance_requirements,
            )
        except SpecialistSelectionError as exc:
            await self._emit(
                "specialist_selection_rejected",
                {"blockers": [blocker.model_dump(mode="json") for blocker in exc.blockers]},
            )
            raise
        self._selected = resolved
        await self._emit(
            "specialist_selection_accepted",
            {
                "selection_rationale": call.selection_rationale,
                "assignments": [metadata.model_dump(mode="json") for metadata in resolved.assignments],
            },
        )
        return resolved

    async def invoke_specialist(
        self,
        call: InvokeSpecialistCall,
        *,
        satisfied_dependency_ids: Sequence[str] = (),
    ) -> ResolvedAssignmentMetadata:
        """Approve one selected assignment after all selected dependencies exist.

        Actual node execution remains owned by the bounded work-graph runtime.
        This call records the leader's explicit invocation without granting any
        additional tool, skill, credential, or policy field.
        """

        if self._selected is None:
            raise SpecialistSelectionError((SpecialistSelectionBlocker(
                code="specialist_selection_missing",
                message="No specialist selection has been accepted.",
                assignment_id=call.assignment_id,
            ),))
        metadata_by_id = {item.assignment_id: item for item in self._selected.assignments}
        metadata = metadata_by_id.get(call.assignment_id)
        if metadata is None:
            raise SpecialistSelectionError((SpecialistSelectionBlocker(
                code="unknown_specialist_assignment",
                message=f"Assignment {call.assignment_id!r} is not part of the accepted selection.",
                assignment_id=call.assignment_id,
            ),))
        unmet = sorted(set(metadata.depends_on) - set(satisfied_dependency_ids))
        if unmet:
            raise SpecialistSelectionError((SpecialistSelectionBlocker(
                code="specialist_dependencies_unmet",
                message=f"Assignment {call.assignment_id!r} cannot start before dependencies {unmet} complete.",
                assignment_id=call.assignment_id,
                template_id=metadata.template_id,
            ),))
        self._invoked_assignment_ids.add(call.assignment_id)
        await self._emit("specialist_invocation_approved", metadata.model_dump(mode="json"))
        return metadata

    def _resolve(
        self,
        call: SelectSpecialistsCall,
        *,
        task_summary: str,
        required_capabilities: Sequence[str] = (),
        required_artifacts: Sequence[str] = (),
        required_acceptance_requirements: Sequence[str] = (),
    ) -> ResolvedSpecialistSelection:
        blockers: list[SpecialistSelectionBlocker] = []
        owned_artifacts = {
            artifact.strip().replace("\\", "/")
            for assignment in call.assignments
            for artifact in assignment.owned_artifacts
            if artifact.strip()
        }
        missing_artifacts = sorted({
            artifact.strip().replace("\\", "/")
            for artifact in required_artifacts
            if artifact.strip()
        } - owned_artifacts)
        if missing_artifacts:
            blockers.append(SpecialistSelectionBlocker(
                code="required_artifacts_unowned",
                message=f"No producer owns required artifacts: {missing_artifacts}.",
            ))
        required_artifact_set = {
            artifact.strip().replace("\\", "/")
            for artifact in required_artifacts
            if artifact.strip()
        }
        unexpected_artifacts = sorted(owned_artifacts - required_artifact_set) if required_artifact_set else []
        if unexpected_artifacts:
            blockers.append(SpecialistSelectionBlocker(
                code="unexpected_artifact_ownership",
                message=f"Producer ownership contains paths outside the required artifact contract: {unexpected_artifacts}.",
            ))
        if required_artifact_set:
            for assignment in call.assignments:
                referenced_paths = {
                    match.replace("\\", "/")
                    for requirement in assignment.acceptance_requirements
                    for match in _ARTIFACT_PATH_PATTERN.findall(requirement)
                }
                outside_contract = sorted(referenced_paths - required_artifact_set)
                if outside_contract:
                    blockers.append(SpecialistSelectionBlocker(
                        code="acceptance_artifact_outside_contract",
                        message=(
                            f"Assignment {assignment.assignment_id!r} acceptance requirements reference paths outside "
                            f"the required artifact contract: {outside_contract}."
                        ),
                        assignment_id=assignment.assignment_id,
                        template_id=assignment.template_id,
                    ))
        bundles: dict[str, ResolvedSpecialistBundle] = {}
        for assignment in call.assignments:
            template = get_fixed_specialist_template(assignment.template_id)
            if template is None:
                blockers.append(SpecialistSelectionBlocker(
                    code="unknown_specialist_template",
                    message=f"Unknown specialist template {assignment.template_id!r}.",
                    assignment_id=assignment.assignment_id,
                    template_id=assignment.template_id,
                ))
                continue
            if assignment.owned_artifacts and not template.artifact_contract.can_produce:
                blockers.append(SpecialistSelectionBlocker(
                    code="specialist_cannot_produce_artifacts",
                    message=(
                        f"Template {assignment.template_id!r} is validation-only and cannot own artifacts: "
                        f"{sorted(assignment.owned_artifacts)}."
                    ),
                    assignment_id=assignment.assignment_id,
                    template_id=assignment.template_id,
                ))
                continue
            granted_tool_ids = set(template.tool_ids)
            for artifact in assignment.owned_artifacts:
                media_kind = _generated_media_kind(artifact)
                if media_kind == "image" and not _IMAGE_GENERATION_TOOL_IDS.issubset(granted_tool_ids):
                    blockers.append(SpecialistSelectionBlocker(
                        code="specialist_media_artifact_capability_unavailable",
                        message=(
                            f"Template {assignment.template_id!r} cannot own generated image artifact {artifact!r}: "
                            "its immutable bundle lacks generate_images and publish_image. Remove the media artifact and report "
                            "a missing_system_capability blocker, or select an available media-capable template."
                        ),
                        assignment_id=assignment.assignment_id,
                        template_id=assignment.template_id,
                    ))
                if media_kind == "video" and not _VIDEO_GENERATION_TOOL_IDS.issubset(granted_tool_ids):
                    blockers.append(SpecialistSelectionBlocker(
                        code="specialist_media_artifact_capability_unavailable",
                        message=(
                            f"Template {assignment.template_id!r} cannot own generated video artifact {artifact!r}: "
                            "its immutable bundle lacks submit_text_to_video and collect_video. Remove the media artifact and report "
                            "a missing_system_capability blocker, or select an available media-capable template."
                        ),
                        assignment_id=assignment.assignment_id,
                        template_id=assignment.template_id,
                    ))
            missing = sorted(set(template.tool_ids) - self._available_tool_ids)
            if missing:
                blockers.append(SpecialistSelectionBlocker(
                    code="specialist_tools_unavailable",
                    message=f"Template {assignment.template_id!r} requires unavailable tools: {missing}.",
                    assignment_id=assignment.assignment_id,
                    template_id=assignment.template_id,
                ))
                continue
            try:
                bundles[assignment.assignment_id] = resolve_specialist_bundle(assignment.template_id)
            except ValueError as exc:
                code = str(exc).split(":", 1)[0]
                blockers.append(SpecialistSelectionBlocker(
                    code=code,
                    message=str(exc),
                    assignment_id=assignment.assignment_id,
                    template_id=assignment.template_id,
                ))
        if blockers:
            raise SpecialistSelectionError(blockers)

        selections_by_id = {item.assignment_id: item for item in call.assignments}
        plan_assignments: list[TeamAssignment] = []
        work_graph: list[WorkNode] = []
        metadata: list[ResolvedAssignmentMetadata] = []
        for selection in call.assignments:
            bundle = bundles[selection.assignment_id]
            template: SpecialistTemplate = bundle.template
            validates = tuple(
                dependency_id
                for dependency_id in selection.depends_on
                if template.artifact_contract.can_validate
                and selections_by_id[dependency_id].owned_artifacts
            )
            acceptance_checks = list(selection.acceptance_requirements)
            if validates and "independent_validation" not in acceptance_checks:
                acceptance_checks.append("independent_validation")
            if validates:
                validation_registry = fixed_template_validation_registry()
                for target_id in validates:
                    producer = selections_by_id[target_id]
                    producer_registration = validation_registry.get(producer.template_id)
                    for required_check in getattr(producer_registration, "required_validation_checks", []):
                        if required_check not in acceptance_checks:
                            acceptance_checks.append(required_check)
                for requirement in required_acceptance_requirements:
                    normalized_requirement = str(requirement).strip()
                    if normalized_requirement and normalized_requirement not in acceptance_checks:
                        acceptance_checks.append(normalized_requirement)
            plan_assignments.append(TeamAssignment(
                id=selection.assignment_id,
                agent_template_id=selection.template_id,
                objective=selection.objective,
                required_capabilities=list(template.capabilities),
                tool_grants=[ToolGrant(
                    capability=template.capabilities[0],
                    tool_ids=list(template.tool_ids),
                    constraints={
                        "template_version": template.version,
                        "tool_bundle_hash": bundle.tool_bundle_hash,
                    },
                )],
                owned_paths=list(selection.owned_artifacts),
                expected_artifacts=list(selection.owned_artifacts),
                acceptance_checks=acceptance_checks,
                validates_assignment_ids=list(validates),
                template_version=template.version,
                tool_bundle_hash=bundle.tool_bundle_hash,
                resolved_skills=[
                    ResolvedSkillBinding(
                        skill_id=skill.skill_id,
                        version=skill.version,
                        sha256=skill.sha256,
                    )
                    for skill in bundle.skills
                ],
            ))
            work_graph.append(WorkNode(
                id=selection.assignment_id,
                assignment_id=selection.assignment_id,
                depends_on=list(selection.depends_on),
                conflict_domains=list(template.conflict_domains),
                estimated_cost_class="low",
            ))
            metadata.append(ResolvedAssignmentMetadata(
                assignment_id=selection.assignment_id,
                template_id=template.template_id,
                template_version=template.version,
                objective=selection.objective,
                capabilities=template.capabilities,
                tool_ids=template.tool_ids,
                tool_bundle_hash=bundle.tool_bundle_hash,
                skill_ids=tuple(skill.skill_id for skill in bundle.skills),
                skill_versions=tuple(skill.version for skill in bundle.skills),
                skill_hashes=tuple(skill.sha256 for skill in bundle.skills),
                depends_on=tuple(selection.depends_on),
                owned_artifacts=tuple(selection.owned_artifacts),
                acceptance_requirements=tuple(acceptance_checks),
                validates_assignment_ids=validates,
            ))

        plan = TeamCompositionPlan(
            task_summary=task_summary,
            assignments=plan_assignments,
            work_graph=work_graph,
            omitted_capabilities=[],
            selection_rationale=call.selection_rationale,
        )
        try:
            validate_team_composition_plan(
                plan,
                fixed_template_validation_registry(),
                available_tool_ids=self._available_tool_ids,
                available_agent_template_ids=set(list_fixed_specialist_templates()),
                required_capabilities=required_capabilities,
            )
        except TeamCompositionValidationError as exc:
            raise SpecialistSelectionError(tuple(
                SpecialistSelectionBlocker(
                    code=issue.code,
                    message=issue.message,
                    assignment_id=issue.assignment_id,
                )
                for issue in exc.issues
            )) from exc
        return ResolvedSpecialistSelection(plan=plan, assignments=tuple(metadata))


def _generated_media_kind(artifact_path: str) -> str | None:
    """Classify generated-media ownership while exempting browser screenshots."""

    normalized = str(artifact_path).strip().replace("\\", "/").lower()
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part]
    if "screenshots" in parts:
        return None
    suffix = Path(normalized).suffix
    if suffix in _IMAGE_ARTIFACT_SUFFIXES:
        return "image"
    if suffix in _VIDEO_ARTIFACT_SUFFIXES:
        return "video"
    return None


def fixed_template_validation_registry() -> dict[str, Any]:
    """Return legacy validation records for fixed templates during migration."""

    registry = dict(CORE_ROLE_CAPABILITIES)
    # ``test_engineer`` remains in the existing dynamic specialist registry;
    # importing lazily avoids exposing that mutable map as the fixed catalog.
    from .capability_registry import list_specialist_templates

    registry.update(list_specialist_templates())
    return registry
