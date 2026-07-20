"""Deterministic private evaluators for Layer B development fixtures."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from .artifacts import hash_file
from .models import EVALUATOR_VERSION, EvaluatorCheck, EvaluatorResult, ScenarioId
from .fixtures import FIXTURES_ROOT


def evaluate_attempt(scenario_id: ScenarioId, workspace_dir: Path) -> EvaluatorResult:
    """Evaluate one workspace copy with the private scenario rules."""

    if scenario_id == "incident_repair":
        return _evaluate_incident_repair(workspace_dir)
    if scenario_id == "screenshot_to_product":
        return _evaluate_screenshot_to_product(workspace_dir)
    raise KeyError(scenario_id)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _check(name: str, passed: bool, points: float, **detail: Any) -> EvaluatorCheck:
    return EvaluatorCheck(name=name, passed=passed, points=points if passed else 0.0, detail=detail)


def _normalized_status(value: Any) -> str:
    """Return one deterministic lowercase status token."""

    return str(value or "").strip().lower().replace("-", "_")


def _test_report_passed(report: dict[str, Any]) -> bool:
    """Accept equivalent machine-readable test report shapes without prose inference."""

    if report.get("passed") is True:
        return True
    status = _normalized_status(report.get("test_status") or report.get("overall_status") or report.get("status"))
    count = report.get("executed_test_count", report.get("tests_executed", report.get("tests_run", 0)))
    tests = report.get("tests", report.get("test_results", report.get("results", [])))
    if status not in {"pass", "passed", "success", "successful"}:
        return False
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return False
    if isinstance(tests, list) and tests:
        return all(
            isinstance(item, dict)
            and _normalized_status(item.get("status") or item.get("result")) in {"pass", "passed", "success", "successful"}
            for item in tests
        )
    retained = report.get("failures_retained")
    return isinstance(retained, list) and bool(retained) and all(
        isinstance(item, dict) and _normalized_status(item.get("status_after_fix")) in {"closed", "fixed", "resolved", "remediated"}
        for item in retained
    )


def _reachable_vulnerability_closed(report: dict[str, Any]) -> bool:
    """Require an explicit structured closure assertion for the reachable issue."""

    if any(report.get(key) is True for key in (
        "reachable_vulnerability_closed",
        "reachable_dependency_issue_closed",
        "vulnerability_closed",
        "dependency_issue_closed",
    )):
        details = report.get("vulnerability_details")
        if report.get("dependency_issue_closed") is not True or not isinstance(details, dict):
            return True
        return details.get("reachable") is True and bool(details.get("upgraded_to"))
    issue = report.get("dependency_issue")
    if isinstance(issue, dict) and issue.get("reachable") is True:
        return _normalized_status(issue.get("status")) in {"closed", "fixed", "remediated"}
    for key in ("vulnerabilities_remediated", "vulnerabilities", "findings"):
        remediated = report.get(key)
        if isinstance(remediated, list) and remediated:
            return all(
                isinstance(item, dict)
                and _normalized_status(item.get("status")) in {"closed", "fixed", "remediated"}
                and item.get("reachable", True) is True
                for item in remediated
            )
    dependency_issues = report.get("dependency_issues")
    if isinstance(dependency_issues, list) and dependency_issues:
        return all(
            isinstance(item, dict)
            and item.get("reachable") is True
            and (
                item.get("closed") is True
                or _normalized_status(item.get("status")) in {"closed", "fixed", "remediated", "upgraded"}
            )
            for item in dependency_issues
        )
    return False


def _rollback_validated(report: dict[str, Any]) -> bool:
    """Accept equivalent explicit rollback-limit validation fields."""

    nested = report.get("rollback_plan")
    if isinstance(nested, dict):
        report = nested
    limit = report.get("max_rollback_minutes", report.get("rollback_limit_minutes"))
    if limit is None:
        limit = report.get("max_rollback_duration_minutes", report.get("max_duration_minutes"))
    if limit is None:
        limit = report.get("validated_max_rollback_duration_minutes")
    if limit != 15:
        return False
    if report.get("validated") is True:
        return True
    validated_max = report.get(
        "validated_max_rollback_duration_minutes",
        report.get("validated_max_duration_minutes"),
    )
    total = report.get("total_estimated_minutes")
    if total is None:
        total_seconds = report.get("total_estimated_seconds")
        if isinstance(total_seconds, (int, float)) and not isinstance(total_seconds, bool):
            total = total_seconds / 60.0
    steps = report.get("steps") or report.get("rollback_steps")
    if (
        isinstance(validated_max, (int, float))
        and not isinstance(validated_max, bool)
        and validated_max <= limit
    ):
        if total is None:
            return bool(steps)
        return isinstance(total, (int, float)) and not isinstance(total, bool) and total <= limit and bool(steps)
    within = any(report.get(key) is True for key in ("within_constraint", "within_time_limit", "within_limit"))
    return within and isinstance(total, (int, float)) and not isinstance(total, bool) and total <= limit and bool(steps)


def _truthful_failure_field_present(report: dict[str, Any]) -> bool:
    """Require an explicit boolean or structured retained-failure field."""

    if report.get("truthful_failures_retained") is True:
        return True
    return any(key in report and isinstance(report.get(key), list) for key in ("failures_retained", "failures"))


def _string_list(report: dict[str, Any], *keys: str) -> list[str]:
    """Return the first explicit list of strings from equivalent field names."""

    for key in keys:
        value = report.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
    return []


def _changed_file_basenames(report: dict[str, Any]) -> list[str]:
    """Return explicit changed-file basenames from common structured report forms."""

    direct = _string_list(report, "changed_files", "changed_file_basenames")
    if direct:
        return [value.replace("\\", "/").rsplit("/", 1)[-1] for value in direct]
    issues = report.get("issues_resolved")
    if not isinstance(issues, list):
        return []
    paths = [
        item.get("file")
        for item in issues
        if isinstance(item, dict) and isinstance(item.get("file"), str) and item.get("file")
    ]
    return [value.replace("\\", "/").rsplit("/", 1)[-1] for value in paths]


def _worker_declares_duplicate_outcome(source: str) -> bool:
    """Require the preserved entry point to declare a non-processing duplicate outcome."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "process_job":
            continue
        outcomes = {
            child.value.value.strip().lower()
            for child in ast.walk(node)
            if isinstance(child, ast.Return)
            and isinstance(child.value, ast.Constant)
            and isinstance(child.value.value, str)
        }
        return any(
            outcome != "processed" and any(token in outcome for token in ("duplicate", "ignored", "skipped"))
            for outcome in outcomes
        )
    return False


def _evaluate_incident_repair(workspace_dir: Path) -> EvaluatorResult:
    repo = workspace_dir / "repo"
    reports = workspace_dir / "reports"
    rollback = _load_json(workspace_dir / "incident" / "rollback_plan.json")
    test_report = _load_json(reports / "test_report.json")
    security_report = _load_json(reports / "security_report.json")
    evidence_report = _load_json(reports / "evidence_report.json")
    cleanup = _load_json(workspace_dir / "cleanup" / "cleanup.json")

    auth_text = (repo / "services" / "api" / "auth.py").read_text(encoding="utf-8")
    worker_text = (repo / "services" / "worker" / "idempotency.py").read_text(encoding="utf-8")
    db_text = (repo / "services" / "config" / "database.py").read_text(encoding="utf-8")
    requirements_text = (repo / "requirements.txt").read_text(encoding="utf-8")

    checks = [
        _check(
            "functional_regression",
            _test_report_passed(test_report)
            and "allow_support_scope" in auth_text
            and _worker_declares_duplicate_outcome(worker_text),
            40,
            report=test_report,
        ),
        _check(
            "security_dependency",
            _reachable_vulnerability_closed(security_report) and "pyyaml==6.0.2" in requirements_text,
            15,
            report=security_report,
        ),
        _check(
            "diagnosis_evidence_accuracy",
            sorted(_string_list(evidence_report, "evidence_ids", "evidence_ids_cited")) == ["AUTH-LOG-401", "DB-CONFIG-URL", "JOB-TRACE-008"],
            15,
            report=evidence_report,
        ),
        _check(
            "rollback_constraints",
            _rollback_validated(rollback),
            10,
            rollback=rollback,
        ),
        _check(
            "patch_scope_and_integration",
            "DB_URL" in db_text
            and "auth.py" in _changed_file_basenames(evidence_report)
            and cleanup.get("workspace_removed") is False,
            10,
            cleanup=cleanup,
        ),
        _check(
            "artifact_provenance",
            cleanup.get("artifact_manifest_complete") is True and bool(evidence_report.get("command_evidence")),
            5,
            cleanup=cleanup,
        ),
        _check(
            "truthful_blockers",
            _truthful_failure_field_present(test_report) and _truthful_failure_field_present(security_report),
            5,
            test_report=test_report,
            security_report=security_report,
        ),
    ]
    mandatory = checks[0].passed and checks[1].passed and checks[3].passed
    total = sum(check.points for check in checks)
    if not mandatory:
        total = min(total, 59.0)
    return EvaluatorResult(
        evaluator_version=EVALUATOR_VERSION,
        scenario_id="incident_repair",
        passed=mandatory and total >= 80.0,
        total_score=total,
        mandatory_gates_passed=mandatory,
        checks=checks,
        aesthetic_score=None,
    )


def _evaluate_screenshot_to_product(workspace_dir: Path) -> EvaluatorResult:
    reference_root = FIXTURES_ROOT / "screenshot_to_product" / "dev" / "public" / "references"
    expected_desktop_hash = hash_file(reference_root / "desktop.png")
    expected_mobile_hash = hash_file(reference_root / "mobile.png")
    reports = workspace_dir / "reports"
    build_report = _load_json(reports / "build.json")
    interaction_report = _load_json(reports / "interaction.json")
    layout_report = _load_json(reports / "layout.json")
    a11y_report = _load_json(reports / "a11y.json")
    copy_manifest = _load_json(reports / "copy_manifest.json")
    provenance = _load_json(reports / "provenance.json")
    desktop_hash = hash_file(workspace_dir / "screenshots" / "desktop.png")
    mobile_hash = hash_file(workspace_dir / "screenshots" / "mobile.png")
    hero_hash = hash_file(workspace_dir / "app" / "dist" / "assets" / "hero-card.png")

    checks = [
        _check(
            "build_interaction_contract",
            bool(build_report.get("passed")) and bool(interaction_report.get("core_flow_passed")) and bool(interaction_report.get("api_contract_passed")),
            25,
            build=build_report,
            interaction=interaction_report,
        ),
        _check(
            "responsive_geometry",
            layout_report.get("desktop", {}).get("hero_width") == 960 and layout_report.get("mobile", {}).get("hero_width") == 320,
            20,
            layout=layout_report,
        ),
        _check(
            "accessibility",
            a11y_report.get("critical_violations") == 0,
            15,
            a11y=a11y_report,
        ),
        _check(
            "asset_dimensions_formats_placement",
            provenance.get("hero_asset", {}).get("dimensions") == [256, 144]
            and provenance.get("hero_asset", {}).get("format") == "png"
            and provenance.get("hero_asset", {}).get("sha256") == hero_hash,
            15,
            provenance=provenance,
        ),
        _check(
            "factual_copy_source_ids",
            copy_manifest.get("source_ids") == ["COPY-HERO-001", "COPY-FEATURE-002", "COPY-CTA-003"],
            10,
            copy=copy_manifest,
        ),
        _check(
            "visual_similarity",
            desktop_hash == expected_desktop_hash and mobile_hash == expected_mobile_hash,
            10,
            desktop_hash=desktop_hash,
            mobile_hash=mobile_hash,
        ),
        _check(
            "artifact_provenance",
            provenance.get("asset_manifest_complete") is True and provenance.get("screenshots_recorded") is True,
            5,
            provenance=provenance,
        ),
    ]
    mandatory = checks[0].passed and checks[2].passed and checks[3].passed
    total = sum(check.points for check in checks)
    if not mandatory:
        total = min(total, 59.0)
    return EvaluatorResult(
        evaluator_version=EVALUATOR_VERSION,
        scenario_id="screenshot_to_product",
        passed=mandatory and total >= 80.0,
        total_score=total,
        mandatory_gates_passed=mandatory,
        checks=checks,
        aesthetic_score=None,
    )
