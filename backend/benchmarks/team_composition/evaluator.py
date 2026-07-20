"""Deterministic evaluator for Layer A team-composition benchmark attempts."""

from __future__ import annotations

from typing import Any

from benchmarks.team_composition.models import CompositionEvaluation, MetricResult, PrivateScenarioLabel
from society.capability_registry import get_role_capabilities
from society.schemas.team_composition import (
    CompositionLimits,
    PlanValidationIssue,
    TeamAssignment,
    TeamCompositionPlan,
    TeamCompositionValidationError,
    WorkNode,
    validate_team_composition_plan,
)
from society.team_composer import TeamCompositionBlocked


def _registry_mapping() -> dict[str, Any]:
    roles = [
        "coordinator",
        "architect",
        "researcher",
        "builder",
        "frontend_engineer",
        "test_engineer",
        "image_creator",
        "video_producer",
        "data_analyst",
        "critic",
    ]
    return {role: registration for role in roles if (registration := get_role_capabilities(role)) is not None}


REGISTRY = _registry_mapping()
_COST_UNITS = {"low": 1, "medium": 2, "high": 3}
_VALIDATOR_CODES = {
    "artifact_without_independent_validation",
    "missing_required_validation_check",
    "missing_acceptance_checks",
    "self_validation",
}
_DEPENDENCY_CODES = {"unknown_dependency", "cyclic_work_graph", "assignment_node_count"}
_CONFLICT_CODES = {"concurrent_work_conflict"}
_ASSIGNMENT_TOOL_CODES = {
    "assignment_capability_mismatch",
    "grant_capability_mismatch",
    "missing_required_tool",
    "forbidden_tool_grant",
    "unavailable_tool_grant",
    "unavailable_agent_template",
    "unknown_agent_template",
}


def _collect_validation_issues(
    plan: TeamCompositionPlan,
    *,
    required_capabilities: list[str],
    available_tool_ids: list[str] | None,
    available_agent_template_ids: list[str] | None,
    limits: CompositionLimits,
) -> list[PlanValidationIssue]:
    try:
        validate_team_composition_plan(
            plan,
            REGISTRY,
            required_capabilities=required_capabilities,
            available_tool_ids=available_tool_ids,
            available_agent_template_ids=available_agent_template_ids,
            limits=limits,
        )
    except TeamCompositionValidationError as exc:
        return [issue.model_copy(deep=True) for issue in exc.issues]
    return []


def _selected_capabilities(
    plan: TeamCompositionPlan,
    *,
    issues: list[PlanValidationIssue],
) -> set[str]:
    selected: set[str] = set()
    mismatched_assignments = {
        issue.assignment_id
        for issue in issues
        if issue.code == "assignment_capability_mismatch" and issue.assignment_id is not None
    }
    for assignment in plan.assignments:
        if assignment.id in mismatched_assignments:
            continue
        registration = REGISTRY.get(assignment.agent_template_id)
        if registration is None:
            continue
        selected.update(registration.capabilities)
    return selected


def _selected_tool_ids(plan: TeamCompositionPlan) -> set[str]:
    selected: set[str] = set()
    for assignment in plan.assignments:
        for grant in assignment.tool_grants:
            selected.update(grant.tool_ids)
    return selected


def _projected_cost_units(plan: TeamCompositionPlan) -> int:
    return sum(_COST_UNITS[node.estimated_cost_class] for node in plan.work_graph)


def _dependency_pairs(plan: TeamCompositionPlan) -> set[tuple[str, str]]:
    assignments = {assignment.id: assignment for assignment in plan.assignments}
    nodes = {node.id: node for node in plan.work_graph}
    pairs: set[tuple[str, str]] = set()
    for node in plan.work_graph:
        consumer = assignments.get(node.assignment_id)
        if consumer is None:
            continue
        for dependency_id in node.depends_on:
            producer_node = nodes.get(dependency_id)
            if producer_node is None:
                continue
            producer = assignments.get(producer_node.assignment_id)
            if producer is None:
                continue
            pairs.add((producer.agent_template_id, consumer.agent_template_id))
    return pairs


def _validator_coverage(plan: TeamCompositionPlan, label: PrivateScenarioLabel) -> tuple[bool, dict[str, Any]]:
    assignments = {assignment.id: assignment for assignment in plan.assignments}
    covered_targets: list[str] = []
    missing_targets: list[str] = []
    details: list[dict[str, Any]] = []
    for requirement in label.required_validator_requirements:
        requirement_met = False
        for assignment in plan.assignments:
            if assignment.agent_template_id not in requirement.allowed_validator_template_ids:
                continue
            targets = [assignments[target_id] for target_id in assignment.validates_assignment_ids if target_id in assignments]
            target_template_ids = {target.agent_template_id for target in targets}
            if not target_template_ids.intersection(requirement.target_template_ids):
                continue
            if not set(requirement.required_checks).issubset(set(assignment.acceptance_checks)):
                continue
            requirement_met = True
            covered_targets.extend(sorted(target_template_ids.intersection(requirement.target_template_ids)))
            details.append({
                "validator_assignment_id": assignment.id,
                "validator_template_id": assignment.agent_template_id,
                "covered_target_template_ids": sorted(target_template_ids),
            })
        if not requirement_met:
            missing_targets.extend(requirement.target_template_ids)
    return not missing_targets, {
        "covered_target_template_ids": sorted(set(covered_targets)),
        "missing_target_template_ids": sorted(set(missing_targets)),
        "details": details,
    }


def _unnecessary_assignments(plan: TeamCompositionPlan, label: PrivateScenarioLabel) -> list[str]:
    useful_capabilities = set(label.required_capabilities).union(label.optional_capabilities)
    validator_templates = {
        template_id
        for requirement in label.required_validator_requirements
        for template_id in requirement.allowed_validator_template_ids
    }
    unnecessary: list[str] = []
    for assignment in plan.assignments:
        contributes_capability = bool(set(assignment.required_capabilities).intersection(useful_capabilities))
        contributes_validation = assignment.agent_template_id in validator_templates and bool(assignment.validates_assignment_ids)
        if contributes_capability or contributes_validation:
            continue
        unnecessary.append(assignment.id)
    return unnecessary


def evaluate_plan(
    plan: TeamCompositionPlan,
    label: PrivateScenarioLabel,
    *,
    available_tool_ids: list[str] | None,
    available_agent_template_ids: list[str] | None,
    limits: CompositionLimits,
    validation_issue_history: list[list[PlanValidationIssue]] | None = None,
) -> CompositionEvaluation:
    """Evaluate a plan against the private label set."""

    issues = _collect_validation_issues(
        plan,
        required_capabilities=label.required_capabilities,
        available_tool_ids=available_tool_ids,
        available_agent_template_ids=available_agent_template_ids,
        limits=limits,
    )
    selected_capabilities = _selected_capabilities(plan, issues=issues)
    missing_required = sorted(set(label.required_capabilities) - selected_capabilities)
    forbidden_selected_capabilities = sorted(set(label.forbidden_capabilities).intersection(selected_capabilities))
    unnecessary_assignments = _unnecessary_assignments(plan, label)
    forbidden_grants = sorted(set(label.forbidden_tool_grants).intersection(_selected_tool_ids(plan)))
    validator_passed, validator_detail = _validator_coverage(plan, label)
    dependency_pairs = _dependency_pairs(plan)
    required_pairs = {(edge.producer_template_id, edge.consumer_template_id) for edge in label.required_dependency_edges}
    allowed_pairs = {(edge.producer_template_id, edge.consumer_template_id) for edge in label.allowed_dependency_edges}
    unexpected_dependency_pairs = sorted(dependency_pairs - allowed_pairs) if allowed_pairs else []
    missing_dependency_pairs = sorted(required_pairs - dependency_pairs)
    conflict_issue_codes = sorted({issue.code for issue in issues if issue.code in _CONFLICT_CODES})
    assignment_tool_issue_codes = sorted({issue.code for issue in issues if issue.code in _ASSIGNMENT_TOOL_CODES})
    dependency_issue_codes = sorted({issue.code for issue in issues if issue.code in _DEPENDENCY_CODES})
    validator_issue_codes = sorted({issue.code for issue in issues if issue.code in _VALIDATOR_CODES})
    selected_assignments = len(plan.assignments)
    projected_cost = _projected_cost_units(plan)

    metrics = [
        MetricResult(
            name="required_capability_recall",
            passed=not missing_required,
            value=((len(label.required_capabilities) - len(missing_required)) / len(label.required_capabilities))
            if label.required_capabilities else 1.0,
            detail={
                "required": label.required_capabilities,
                "selected": sorted(selected_capabilities),
                "selected_template_ids": sorted({assignment.agent_template_id for assignment in plan.assignments}),
                "missing": missing_required,
            },
        ),
        MetricResult(
            name="unnecessary_capability_rate_count",
            passed=len(unnecessary_assignments) <= 1,
            value=len(unnecessary_assignments),
            detail={
                "unnecessary_assignment_ids": unnecessary_assignments,
                "unnecessary_rate": (len(unnecessary_assignments) / selected_assignments) if selected_assignments else 0.0,
                "forbidden_selected_capabilities": forbidden_selected_capabilities,
            },
        ),
        MetricResult(
            name="forbidden_grant_violations",
            passed=not forbidden_grants,
            value=len(forbidden_grants),
            detail={"forbidden_grant_tool_ids": forbidden_grants},
        ),
        MetricResult(
            name="assignment_tool_compatibility",
            passed=not assignment_tool_issue_codes,
            value=len(assignment_tool_issue_codes),
            detail={"issue_codes": assignment_tool_issue_codes},
        ),
        MetricResult(
            name="independent_validator_coverage",
            passed=validator_passed and not validator_issue_codes,
            value=validator_passed,
            detail={**validator_detail, "issue_codes": validator_issue_codes},
        ),
        MetricResult(
            name="dependency_correctness",
            passed=not missing_dependency_pairs and not unexpected_dependency_pairs and not dependency_issue_codes,
            value=not missing_dependency_pairs and not unexpected_dependency_pairs,
            detail={
                "required_pairs": sorted(required_pairs),
                "selected_pairs": sorted(dependency_pairs),
                "missing_pairs": missing_dependency_pairs,
                "unexpected_pairs": unexpected_dependency_pairs,
                "issue_codes": dependency_issue_codes,
            },
        ),
        MetricResult(
            name="conflict_correctness",
            passed=not conflict_issue_codes,
            value=not conflict_issue_codes,
            detail={"issue_codes": conflict_issue_codes, "forbidden_parallel_pairs": label.forbidden_parallel_template_pairs},
        ),
        MetricResult(
            name="team_size_projected_cost_efficiency",
            passed=(
                label.team_size_expectation.min_assignments <= selected_assignments <= label.team_size_expectation.max_assignments
                and projected_cost <= label.team_size_expectation.max_projected_cost_units
            ),
            value=selected_assignments,
            detail={
                "selected_assignments": selected_assignments,
                "projected_cost_units": projected_cost,
                "expected": label.team_size_expectation.model_dump(mode="json"),
            },
        ),
        MetricResult(
            name="typed_blocker_correctness",
            passed=label.expected_blocker_category is None,
            value=label.expected_blocker_category is None,
            detail={"expected_blocker_category": label.expected_blocker_category, "actual_blocker_category": None},
        ),
        MetricResult(
            name="recovery_after_unavailable_specialist",
            passed=label.recovery_expectation is None,
            value=label.recovery_expectation is None,
            detail={"recovery_expected": label.recovery_expectation.model_dump(mode="json") if label.recovery_expectation else None},
        ),
    ]
    strict_pass = (
        not missing_required
        and not forbidden_grants
        and not dependency_issue_codes
        and not conflict_issue_codes
        and not missing_dependency_pairs
        and not unexpected_dependency_pairs
        and len(unnecessary_assignments) <= 1
        and label.expected_blocker_category is None
        and (validator_passed if label.required_validator_requirements else True)
    )
    return CompositionEvaluation(
        outcome_type="plan",
        strict_pass=strict_pass,
        metrics=metrics,
        validation_issue_codes=sorted({issue.code for issue in issues}),
        validation_issue_history=[[issue.code for issue in group] for group in (validation_issue_history or [])],
        projected_cost_units=projected_cost,
    )


def evaluate_blocked(blocked: TeamCompositionBlocked, label: PrivateScenarioLabel) -> CompositionEvaluation:
    """Evaluate a truthful blocked result without converting it into a plan."""

    issue_codes = [issue.code for issue in blocked.issues]
    expected_recovery_blocker = (
        label.recovery_expectation.expected_blocker_category
        if label.recovery_expectation is not None
        else None
    )
    expected_blocker_category = label.expected_blocker_category or expected_recovery_blocker
    typed_blocker_passed = expected_blocker_category == blocked.category
    recovery_passed = True
    recovery_detail: dict[str, Any] = {"recovery_expected": None}
    if label.recovery_expectation is not None:
        recovery_detail = {"recovery_expected": label.recovery_expectation.model_dump(mode="json")}
        if label.recovery_expectation.expected_blocker_category is not None:
            recovery_passed = label.recovery_expectation.expected_blocker_category == blocked.category
    metrics = [
        MetricResult(
            name="required_capability_recall",
            passed=False,
            value=0.0,
            detail={"required": label.required_capabilities, "selected": [], "missing": label.required_capabilities},
        ),
        MetricResult(
            name="unnecessary_capability_rate_count",
            passed=True,
            value=0,
            detail={"unnecessary_assignment_ids": [], "unnecessary_rate": 0.0, "forbidden_selected_capabilities": []},
        ),
        MetricResult(name="forbidden_grant_violations", passed=True, value=0, detail={"forbidden_grant_tool_ids": []}),
        MetricResult(name="assignment_tool_compatibility", passed=True, value=0, detail={"issue_codes": []}),
        MetricResult(
            name="independent_validator_coverage",
            passed=not label.required_validator_requirements,
            value=not label.required_validator_requirements,
            detail={"covered_target_template_ids": [], "missing_target_template_ids": [], "details": []},
        ),
        MetricResult(
            name="dependency_correctness",
            passed=True,
            value=True,
            detail={"required_pairs": [], "selected_pairs": [], "missing_pairs": [], "unexpected_pairs": [], "issue_codes": []},
        ),
        MetricResult(
            name="conflict_correctness",
            passed=True,
            value=True,
            detail={"issue_codes": [], "forbidden_parallel_pairs": label.forbidden_parallel_template_pairs},
        ),
        MetricResult(
            name="team_size_projected_cost_efficiency",
            passed=True,
            value=0,
            detail={"selected_assignments": 0, "projected_cost_units": 0, "expected": label.team_size_expectation.model_dump(mode="json")},
        ),
        MetricResult(
            name="typed_blocker_correctness",
            passed=typed_blocker_passed,
            value=typed_blocker_passed,
            detail={
                "expected_blocker_category": expected_blocker_category,
                "top_level_expected_blocker_category": label.expected_blocker_category,
                "recovery_expected_blocker_category": expected_recovery_blocker,
                "actual_blocker_category": blocked.category,
                "issue_codes": issue_codes,
            },
        ),
        MetricResult(
            name="recovery_after_unavailable_specialist",
            passed=recovery_passed,
            value=recovery_passed,
            detail={**recovery_detail, "actual_blocker_category": blocked.category},
        ),
    ]
    return CompositionEvaluation(
        outcome_type="blocked",
        strict_pass=typed_blocker_passed and (expected_blocker_category is not None),
        metrics=metrics,
        validation_issue_codes=issue_codes,
        validation_issue_history=[[issue.code for issue in issue_group] for issue_group in blocked.validation_issue_history],
        projected_cost_units=0,
    )
