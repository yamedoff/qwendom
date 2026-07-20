"""Typed team-composition plans and side-effect-free validation.

The composer and provider adapters are intentionally separate. A proposed
plan must pass the validation in this module before any provider session or
media job can be created.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from society.capability_registry import canonical_tool_id

CostClass = Literal["low", "medium", "high"]


class TeamCompositionEventType(StrEnum):
    """Durable event names introduced by the composition roadmap."""

    PROPOSED = "team_composition_proposed"
    VALIDATED = "team_composition_validated"
    RECOMPOSITION_REQUESTED = "team_recomposition_requested"
    WORK_NODE_STARTED = "work_node_started"
    WORK_NODE_BLOCKED = "work_node_blocked"
    WORK_NODE_FAILED = "work_node_failed"
    WORK_NODE_RETRY_SCHEDULED = "work_node_retry_scheduled"
    WORK_NODE_CANCELED = "work_node_canceled"
    WORK_NODE_COMPLETED = "work_node_completed"
    ARTIFACT_VALIDATED = "artifact_validated"


class ToolGrant(BaseModel):
    """A least-privilege capability and its explicitly granted tool IDs."""

    capability: str = Field(min_length=1)
    tool_ids: list[str] = Field(default_factory=list)
    constraints: dict[str, str | int | bool] = Field(default_factory=dict)

    @field_validator("tool_ids")
    @classmethod
    def tool_ids_are_unique(cls, value: list[str]) -> list[str]:
        """Reject duplicated grants so audit records remain unambiguous."""

        if len(value) != len(set(value)):
            raise ValueError("tool_ids must be unique within a grant")
        return value


class ResolvedSkillBinding(BaseModel):
    """Pinned repository skill metadata added only by the trusted runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TeamAssignment(BaseModel):
    """One specialist's bounded objective, permissions, and output contract."""

    id: str = Field(min_length=1)
    agent_template_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    required_capabilities: list[str] = Field(default_factory=list)
    tool_grants: list[ToolGrant] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    validates_assignment_ids: list[str] = Field(
        default_factory=list,
        description="Assignments whose artifacts this independent reviewer validates.",
    )
    template_version: str | None = None
    tool_bundle_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resolved_skills: list[ResolvedSkillBinding] = Field(default_factory=list)


class WorkNode(BaseModel):
    """A dependency-graph node bound to exactly one team assignment."""

    id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    conflict_domains: list[str] = Field(default_factory=list)
    estimated_cost_class: CostClass = "low"


class TeamCompositionPlan(BaseModel):
    """Persistable team selection and dependency-aware execution proposal."""

    task_summary: str = Field(min_length=1)
    assignments: list[TeamAssignment] = Field(default_factory=list)
    work_graph: list[WorkNode] = Field(default_factory=list)
    omitted_capabilities: list[str] = Field(default_factory=list)
    selection_rationale: str = Field(min_length=1)


class CompositionLimits(BaseModel):
    """Configurable upper bounds applied before provider-backed execution."""

    max_model_workers: int = Field(default=4, ge=1)
    max_agentbay_sessions: int = Field(default=3, ge=0)
    max_media_jobs: int = Field(default=2, ge=0)
    max_dynamic_specialists: int = Field(default=5, ge=0)
    max_total_cost_units: int | None = Field(default=None, ge=0)


class PlanValidationIssue(BaseModel):
    """One stable, machine-readable reason a composition plan was rejected."""

    code: str
    message: str
    assignment_id: str | None = None
    node_id: str | None = None


class TeamCompositionValidationError(ValueError):
    """Raised with every detected issue before external tools are called."""

    def __init__(self, issues: Sequence[PlanValidationIssue]) -> None:
        self.issues = list(issues)
        summary = "; ".join(f"{issue.code}: {issue.message}" for issue in self.issues)
        super().__init__(summary)


def _normalized_path(path: str) -> str:
    """Normalize repository-relative paths for conservative lock comparison."""

    return path.strip().replace("\\", "/").strip("/").casefold()


def _paths_overlap(left: str, right: str) -> bool:
    """Return whether two file or directory ownership declarations overlap."""

    left_path = _normalized_path(left)
    right_path = _normalized_path(right)
    if not left_path or not right_path:
        return False
    return (
        left_path == right_path
        or left_path.startswith(f"{right_path}/")
        or right_path.startswith(f"{left_path}/")
    )


def _normalized_conflict_domains(domains: Sequence[str]) -> set[str]:
    """Normalize conflict-domain labels to match runtime locking semantics."""

    return {domain.strip().casefold() for domain in domains if domain.strip()}


def _topological_layers(nodes: Sequence[WorkNode]) -> tuple[list[list[WorkNode]], set[str]]:
    """Return potential concurrency layers and IDs involved in an invalid DAG."""

    by_id = {node.id: node for node in nodes}
    remaining = set(by_id)
    completed: set[str] = set()
    layers: list[list[WorkNode]] = []
    while remaining:
        ready = [
            by_id[node_id]
            for node_id in sorted(remaining)
            if set(by_id[node_id].depends_on).issubset(completed)
        ]
        if not ready:
            return layers, remaining
        layers.append(ready)
        completed.update(node.id for node in ready)
        remaining.difference_update({node.id for node in ready})
    return layers, set()


def validate_team_composition_plan(
    plan: TeamCompositionPlan,
    registry: Mapping[str, Any],
    *,
    required_capabilities: Collection[str] = (),
    available_tool_ids: Collection[str] | None = None,
    available_agent_template_ids: Collection[str] | None = None,
    limits: CompositionLimits | None = None,
) -> None:
    """Reject an unsafe or incomplete plan without performing side effects.

    Registry values are expected to expose ``capabilities``, ``allowed_tools``,
    ``required_tools``, ``required_validation_checks``, and
    ``can_accept_artifacts`` fields. The loose mapping type keeps this schema
    module independent from the registry implementation and avoids a circular
    import.
    """

    active_limits = limits or CompositionLimits()
    issues: list[PlanValidationIssue] = []
    assignments = {assignment.id: assignment for assignment in plan.assignments}
    available = (
        {canonical_tool_id(tool_id) for tool_id in available_tool_ids}
        if available_tool_ids is not None
        else None
    )
    available_templates = (
        set(available_agent_template_ids)
        if available_agent_template_ids is not None
        else None
    )

    assignment_counts: defaultdict[str, int] = defaultdict(int)
    for assignment in plan.assignments:
        assignment_counts[assignment.id] += 1
    for assignment_id, count in assignment_counts.items():
        if count > 1:
            issues.append(PlanValidationIssue(
                code="duplicate_assignment_id",
                message=f"assignment id {assignment_id!r} appears {count} times",
                assignment_id=assignment_id,
            ))

    granted_capabilities: set[str] = set()
    registration_by_assignment: dict[str, Any] = {}
    for assignment in plan.assignments:
        registration = registry.get(assignment.agent_template_id)
        if registration is None:
            issues.append(PlanValidationIssue(
                code="unknown_agent_template",
                message=f"unknown template {assignment.agent_template_id!r}",
                assignment_id=assignment.id,
            ))
            continue
        if available_templates is not None and assignment.agent_template_id not in available_templates:
            issues.append(PlanValidationIssue(
                code="unavailable_agent_template",
                message=f"template {assignment.agent_template_id!r} is not available in this run",
                assignment_id=assignment.id,
            ))

        registration_by_assignment[assignment.id] = registration
        registered_capabilities = set(registration.capabilities)
        missing_from_role = set(assignment.required_capabilities) - registered_capabilities
        if missing_from_role:
            issues.append(PlanValidationIssue(
                code="assignment_capability_mismatch",
                message=f"template lacks capabilities {sorted(missing_from_role)}",
                assignment_id=assignment.id,
            ))

        granted_capabilities.update(assignment.required_capabilities)
        granted_tools: set[str] = set()
        allowed_tools = {canonical_tool_id(tool_id) for tool_id in registration.allowed_tools}
        for grant in assignment.tool_grants:
            if grant.capability not in registered_capabilities:
                issues.append(PlanValidationIssue(
                    code="grant_capability_mismatch",
                    message=f"grant capability {grant.capability!r} is not registered for the template",
                    assignment_id=assignment.id,
                ))
            granted_tool_ids = {canonical_tool_id(tool_id) for tool_id in grant.tool_ids}
            forbidden = granted_tool_ids - allowed_tools
            if forbidden:
                issues.append(PlanValidationIssue(
                    code="forbidden_tool_grant",
                    message=f"tools are outside the template allowlist: {sorted(forbidden)}",
                    assignment_id=assignment.id,
                ))
            if available is not None:
                unavailable = granted_tool_ids - available
                if unavailable:
                    issues.append(PlanValidationIssue(
                        code="unavailable_tool_grant",
                        message=f"tools are not available in this runtime: {sorted(unavailable)}",
                        assignment_id=assignment.id,
                    ))
            granted_tools.update(granted_tool_ids)

        required_tools = {canonical_tool_id(tool_id) for tool_id in getattr(registration, "required_tools", [])}
        missing_tools = required_tools - granted_tools
        if missing_tools:
            issues.append(PlanValidationIssue(
                code="missing_required_tool",
                message=f"execution contract requires tools {sorted(missing_tools)}",
                assignment_id=assignment.id,
            ))

    uncovered = set(required_capabilities) - granted_capabilities
    if uncovered:
        issues.append(PlanValidationIssue(
            code="unowned_required_capability",
            message=f"no assignment owns required capabilities {sorted(uncovered)}",
        ))

    validator_checks_by_target: defaultdict[str, list[str]] = defaultdict(list)
    for assignment in plan.assignments:
        registration = registration_by_assignment.get(assignment.id)
        for target_id in assignment.validates_assignment_ids:
            if target_id == assignment.id:
                issues.append(PlanValidationIssue(
                    code="self_validation",
                    message="an assignment cannot validate its own artifacts",
                    assignment_id=assignment.id,
                ))
                continue
            if target_id not in assignments:
                issues.append(PlanValidationIssue(
                    code="unknown_validation_target",
                    message=f"validation target {target_id!r} does not exist",
                    assignment_id=assignment.id,
                ))
                continue
            if registration is not None and not getattr(registration, "can_accept_artifacts", False):
                issues.append(PlanValidationIssue(
                    code="assignment_cannot_validate_artifacts",
                    message="template is not authorized to perform final artifact acceptance",
                    assignment_id=assignment.id,
                ))
            validator_checks_by_target[target_id].extend(assignment.acceptance_checks)

        if assignment.validates_assignment_ids and not assignment.acceptance_checks:
            issues.append(PlanValidationIssue(
                code="missing_acceptance_checks",
                message="an independent validator must declare acceptance checks",
                assignment_id=assignment.id,
            ))

    for assignment in plan.assignments:
        registration = registration_by_assignment.get(assignment.id)
        if not assignment.expected_artifacts:
            continue
        if assignment.id not in validator_checks_by_target:
            issues.append(PlanValidationIssue(
                code="artifact_without_independent_validation",
                message="expected artifacts have no independent validator and acceptance checks",
                assignment_id=assignment.id,
            ))
            continue

        required_checks = set(getattr(registration, "required_validation_checks", []))
        present_checks = set(validator_checks_by_target[assignment.id])
        missing_checks = required_checks - present_checks
        if missing_checks:
            issues.append(PlanValidationIssue(
                code="missing_required_validation_check",
                message=f"artifact validation is missing checks {sorted(missing_checks)}",
                assignment_id=assignment.id,
            ))

    node_counts: defaultdict[str, int] = defaultdict(int)
    assignment_node_counts: defaultdict[str, int] = defaultdict(int)
    node_ids = {node.id for node in plan.work_graph}
    for node in plan.work_graph:
        node_counts[node.id] += 1
        assignment_node_counts[node.assignment_id] += 1
    for node_id, count in node_counts.items():
        if count > 1:
            issues.append(PlanValidationIssue(
                code="duplicate_work_node_id",
                message=f"work node id {node_id!r} appears {count} times",
                node_id=node_id,
            ))

    for node in plan.work_graph:
        if node.assignment_id not in assignments:
            issues.append(PlanValidationIssue(
                code="unknown_node_assignment",
                message=f"assignment {node.assignment_id!r} does not exist",
                node_id=node.id,
            ))
        unknown_dependencies = set(node.depends_on) - node_ids
        if unknown_dependencies:
            issues.append(PlanValidationIssue(
                code="unknown_dependency",
                message=f"dependencies do not exist: {sorted(unknown_dependencies)}",
                node_id=node.id,
            ))
        if node.id in node.depends_on:
            issues.append(PlanValidationIssue(
                code="self_dependency",
                message="a work node cannot depend on itself",
                node_id=node.id,
            ))

    for assignment_id in assignments:
        count = assignment_node_counts[assignment_id]
        if count != 1:
            issues.append(PlanValidationIssue(
                code="assignment_node_count",
                message=f"assignment must have exactly one work node; found {count}",
                assignment_id=assignment_id,
            ))

    layers, cyclic_ids = _topological_layers(plan.work_graph)
    if cyclic_ids:
        issues.append(PlanValidationIssue(
            code="cyclic_work_graph",
            message=f"work graph contains a cycle involving {sorted(cyclic_ids)}",
        ))

    execution_capability = "sandbox_execution"
    media_capabilities = {"image_generation", "video_generation"}
    cost_units = {"low": 1, "medium": 2, "high": 3}
    for layer in layers:
        layer_assignments = [
            assignments[node.assignment_id]
            for node in layer
            if node.assignment_id in assignments
        ]
        if len(layer) > active_limits.max_model_workers:
            issues.append(PlanValidationIssue(
                code="model_worker_limit_exceeded",
                message=f"ready layer needs {len(layer)} workers; limit is {active_limits.max_model_workers}",
            ))

        execution_count = sum(
            execution_capability in assignment.required_capabilities
            for assignment in layer_assignments
        )
        if execution_count > active_limits.max_agentbay_sessions:
            issues.append(PlanValidationIssue(
                code="agentbay_session_limit_exceeded",
                message=f"ready layer needs {execution_count} sessions; limit is {active_limits.max_agentbay_sessions}",
            ))

        media_count = sum(
            bool(media_capabilities.intersection(assignment.required_capabilities))
            for assignment in layer_assignments
        )
        if media_count > active_limits.max_media_jobs:
            issues.append(PlanValidationIssue(
                code="media_job_limit_exceeded",
                message=f"ready layer needs {media_count} media jobs; limit is {active_limits.max_media_jobs}",
            ))

        for index, left_node in enumerate(layer):
            left_assignment = assignments.get(left_node.assignment_id)
            if left_assignment is None:
                continue
            for right_node in layer[index + 1:]:
                right_assignment = assignments.get(right_node.assignment_id)
                if right_assignment is None:
                    continue
                overlapping_paths = [
                    (left, right)
                    for left in left_assignment.owned_paths
                    for right in right_assignment.owned_paths
                    if _paths_overlap(left, right)
                ]
                overlapping_domains = _normalized_conflict_domains(left_node.conflict_domains).intersection(
                    _normalized_conflict_domains(right_node.conflict_domains)
                )
                if overlapping_paths or overlapping_domains:
                    issues.append(PlanValidationIssue(
                        code="concurrent_work_conflict",
                        message=(
                            f"ready nodes {left_node.id!r} and {right_node.id!r} conflict "
                            f"on paths {overlapping_paths} or domains {sorted(overlapping_domains)}"
                        ),
                    ))

    dynamic_count = sum(
        assignment.agent_template_id != "coordinator"
        for assignment in plan.assignments
    )
    if dynamic_count > active_limits.max_dynamic_specialists:
        issues.append(PlanValidationIssue(
            code="dynamic_specialist_limit_exceeded",
            message=f"plan selects {dynamic_count} specialists; limit is {active_limits.max_dynamic_specialists}",
        ))

    if active_limits.max_total_cost_units is not None:
        total_cost = sum(cost_units[node.estimated_cost_class] for node in plan.work_graph)
        if total_cost > active_limits.max_total_cost_units:
            issues.append(PlanValidationIssue(
                code="cost_limit_exceeded",
                message=f"plan costs {total_cost} units; limit is {active_limits.max_total_cost_units}",
            ))

    if issues:
        raise TeamCompositionValidationError(issues)
