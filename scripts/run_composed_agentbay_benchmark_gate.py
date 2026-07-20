"""Run one real two-node CompositionRuntime AgentBay benchmark gate.

This runner is intentionally narrow and credential-gated:
- loads `backend/.env` through `Settings`
- validates one fixed two-assignment plan before any provider call
- executes exactly one live CompositionRuntime attempt with real Qwen agents
- writes one atomic evidence JSON record and one concise summary file

It never persists prompts, keys, provider URLs, session IDs, or raw provider
bodies. It also never retries the live run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings
from society.capability_registry import get_role_capabilities
from society.composition_runtime import CompositionRuntime
from society.provider_preflight import model_capability_preflight
from society.schemas.team_composition import (
    TeamAssignment,
    TeamCompositionPlan,
    TeamCompositionValidationError,
    ToolGrant,
    WorkNode,
    validate_team_composition_plan,
)
from society.team_composer import limits_from_settings
from society.work_graph import WorkGraphTerminalStatus, WorkNodeStatus


PHASE_DATE = "2026-07-15"
SUMMARY_PATH_DEFAULT = ROOT / "backend" / "benchmark_results" / "composition-smokes" / PHASE_DATE / "last-message.txt"
EVIDENCE_BASE_DIR = ROOT / "backend" / "benchmark_results" / "composition-smokes" / PHASE_DATE
MAX_EVENT_RECORDS = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def load_settings() -> Settings:
    env_path = BACKEND_DIR / ".env"
    return Settings(
        _env_file=env_path,
        LLM_PROVIDER="qwen",
        QWEN_MODEL="qwen3.7-plus",
        SUBTASK_MAX_ATTEMPTS=1,
        SOCIETY_MAX_MODEL_WORKERS=2,
        SOCIETY_MAX_AGENTBAY_SESSIONS=1,
        SOCIETY_MAX_MEDIA_JOBS=0,
        AGENTBAY_SESSION_TIMEOUT_SECONDS=120,
    )


def _sanitize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value
        for marker in ("Authorization", "authorization", "Bearer ", "api_key", "secret", "password", "session_id", "task_id", "handle"):
            text = text.replace(marker, "[redacted]")
        return text[:500]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            key_text = str(key)
            if any(token in key_text.lower() for token in ("prompt", "instruction", "session", "url", "body", "key", "secret", "handle")):
                continue
            result[key_text] = _sanitize_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in list(value)[:32]]
    return str(value)[:500]


def _make_plan() -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Prove two real composed Qwen specialists can perform bounded AgentBay and local artifact validation work through CompositionRuntime.",
        assignments=[
            TeamAssignment(
                id="builder-assignment",
                agent_template_id="builder",
                objective=(
                    "Create /workspace/calc.py containing exactly one tiny pure Python function named add(a, b) that returns a + b. "
                    "Use the sandbox only. Write the file, run a harmless bounded check, export /workspace/calc.py, and return strict JSON only. "
                    "Required JSON shape: "
                    "{\"artifact_refs\":[\"<durable_local_export_path>\"],"
                    "\"summary\":\"<short summary>\","
                    "\"check_results\":[\"<short check result>\"]}."
                ),
                required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                tool_grants=[
                    ToolGrant(
                        capability="implementation",
                        tool_ids=[
                            "start_execution_environment",
                            "execute_command",
                            "run_code",
                            "write_text_file",
                            "export_artifact",
                            "close_execution_environment",
                        ],
                    )
                ],
                expected_artifacts=["calc.py"],
            ),
            TeamAssignment(
                id="test-assignment",
                agent_template_id="test_engineer",
                objective=(
                    "Read only the direct Builder output. Inspect the exported local artifact with inspect_artifact, then start your own separate sandbox. "
                    "Run independent assertions based on the inspected code without modifying the Builder artifact. "
                    "Call report_independent_validation exactly once and return strict JSON only. "
                    "Required JSON shape: "
                    "{\"artifact_refs\":[\"<inspected_local_artifact_path>\"],"
                    "\"inspected_artifact_refs\":[\"<inspected_local_artifact_path>\"],"
                    "\"passed\":true,"
                    "\"checks\":[\"<short passed check>\"],"
                    "\"failures\":[],"
                    "\"summary\":\"<short summary>\"}."
                ),
                required_capabilities=["test_execution", "sandbox_execution", "artifact_inspection"],
                tool_grants=[
                    ToolGrant(
                        capability="test_execution",
                        tool_ids=[
                            "start_execution_environment",
                            "execute_command",
                            "run_code",
                            "inspect_artifact",
                            "report_independent_validation",
                            "close_execution_environment",
                        ],
                    )
                ],
                acceptance_checks=["artifact_diff_review", "independent_execution"],
                validates_assignment_ids=["builder-assignment"],
            ),
        ],
        work_graph=[
            WorkNode(id="builder-node", assignment_id="builder-assignment", estimated_cost_class="low"),
            WorkNode(id="test-node", assignment_id="test-assignment", depends_on=["builder-node"], estimated_cost_class="low"),
        ],
        selection_rationale="The benchmark requires one bounded builder and one independent non-voting validator in separate AgentBay sandboxes.",
    )


def _artifact_details(path_str: str) -> dict[str, Any]:
    path = Path(path_str).resolve()
    raw = path.read_bytes()
    return {
        "path": str(path),
        "relative_path": str(path.relative_to(ROOT)).replace("\\", "/") if ROOT in path.parents else path.name,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _event_record(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    keep_keys = {
        "assignment_id",
        "node_id",
        "template_id",
        "capabilities",
        "can_vote",
        "attempt",
        "status",
        "started_at",
        "finished_at",
        "duration_seconds",
        "category",
        "code",
        "reason",
        "tool_ids",
        "request_id",
        "closed",
        "idempotent",
        "success",
        "error_code",
        "error_message",
        "error",
        "message",
        "artifact_ref",
        "sha256",
        "size_bytes",
        "passed",
        "failure_count",
        "checks",
        "failures",
        "inspected_artifact_refs",
        "warnings",
        "results",
    }
    record = {"event_type": event_type}
    for key, value in payload.items():
        if key in keep_keys:
            record[key] = _sanitize_value(value)
    return record


def _extract_request_ids(events: list[dict[str, Any]], event_type: str) -> list[str]:
    ids: list[str] = []
    for item in events:
        if item.get("event_type") != event_type:
            continue
        request_id = item.get("request_id")
        if isinstance(request_id, str) and request_id:
            ids.append(request_id)
        for result in item.get("results", []) if isinstance(item.get("results"), list) else []:
            nested = result.get("request_id")
            if isinstance(nested, str) and nested:
                ids.append(nested)
    return ids


def _summarize_node(node: Any) -> dict[str, Any]:
    return {
        "status": node.status.value if hasattr(node.status, "value") else str(node.status),
        "artifact_refs": list(node.artifact_refs),
        "attempts": [
            {
                "attempt": attempt.attempt,
                "status": attempt.status.value if hasattr(attempt.status, "value") else str(attempt.status),
                "category": attempt.category,
                "code": attempt.code,
                "message": attempt.message,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "duration_seconds": attempt.duration_seconds,
            }
            for attempt in node.attempts
        ],
        "started_at": node.started_at,
        "finished_at": node.finished_at,
    }


async def _run_once(output_last_message_path: Path) -> tuple[dict[str, Any], str]:
    started_wall = _utc_now()
    started_monotonic = time.monotonic()
    unique_suffix = f"{started_wall.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
    attempt_dir = EVIDENCE_BASE_DIR / f"composed-agentbay-{unique_suffix}"
    artifact_root = attempt_dir / "artifacts"
    evidence_path = attempt_dir / "evidence.json"
    settings = load_settings()
    preflight = model_capability_preflight(settings)
    runtime = CompositionRuntime(settings, artifact_root)
    plan = _make_plan()
    event_log: list[dict[str, Any]] = []

    def event_sink(event_type: str, payload: dict[str, Any]) -> None:
        if len(event_log) >= MAX_EVENT_RECORDS:
            return
        event_log.append(_event_record(event_type, dict(payload)))

    runtime = CompositionRuntime(settings, artifact_root, event_sink=event_sink)
    validation_error: TeamCompositionValidationError | None = None
    try:
        validate_team_composition_plan(
            plan,
            {
                role: get_role_capabilities(role)
                for role in ("builder", "test_engineer")
            },
            required_capabilities={"implementation", "sandbox_execution", "artifact_export", "test_execution", "artifact_inspection"},
            available_tool_ids=runtime.available_tool_ids().tool_ids,
            available_agent_template_ids=("builder", "test_engineer"),
            limits=limits_from_settings(settings),
        )
    except TeamCompositionValidationError as exc:
        validation_error = exc

    live_result = None
    live_exception: str | None = None
    if validation_error is None and settings.active_model == "qwen3.7-plus" and preflight.get("provider_services", {}).get("agentbay", {}).get("ready"):
        try:
            live_result = await runtime.execute_plan(
                plan,
                task_id="composition-benchmark-gate-2026-07-15",
                user_request=(
                    "Run exactly two sequential non-voting specialists. Builder must create, check, export, and report one tiny Python artifact. "
                    "Test Engineer must inspect that exported artifact locally, run independent assertions in a separate AgentBay sandbox, and report pass/fail JSON."
                ),
            )
        except Exception as exc:  # pragma: no cover - live failure evidence path
            live_exception = f"{type(exc).__name__}: {exc}"

    builder_artifacts: list[dict[str, Any]] = []
    if live_result is not None:
        for artifact_ref in live_result.node_artifact_refs.get("builder-node", []):
            try:
                builder_artifacts.append(_artifact_details(artifact_ref))
            except Exception:
                builder_artifacts.append({"path": artifact_ref, "error": "artifact_unreadable"})

    cleanup_results: list[dict[str, Any]] = [
        item
        for item in event_log
        if item.get("event_type") == "composition_assignment_cleanup_completed"
    ]
    create_request_ids = _extract_request_ids(event_log, "agentbay_start_succeeded")
    delete_request_ids = _extract_request_ids(event_log, "composition_assignment_cleanup_completed")

    success = False
    blockers: list[str] = []
    if validation_error is not None:
        blockers.append("plan_validation_failed")
    if settings.active_model != "qwen3.7-plus":
        blockers.append("model_mismatch")
    if not preflight.get("provider_services", {}).get("agentbay", {}).get("ready"):
        blockers.append("agentbay_not_ready")
    if live_exception is not None:
        blockers.append("live_runtime_exception")
    if live_result is None:
        blockers.append("live_result_missing")
    else:
        graph = live_result.graph_result
        builder_node = graph.nodes.get("builder-node")
        test_node = graph.nodes.get("test-node")
        builder_output = live_result.node_outputs.get("builder-node", {})
        test_output = live_result.node_outputs.get("test-node", {})
        if graph.terminal_status != WorkGraphTerminalStatus.COMPLETED:
            blockers.append("graph_not_completed")
        if builder_node is None or builder_node.status != WorkNodeStatus.COMPLETED:
            blockers.append("builder_not_completed")
        if test_node is None or test_node.status != WorkNodeStatus.COMPLETED:
            blockers.append("test_not_completed")
        if not live_result.node_artifact_refs.get("builder-node"):
            blockers.append("builder_artifact_missing")
        if not bool(test_output.get("passed")):
            blockers.append("independent_validation_not_passed")
        if any(agent.can_vote for agent in live_result.materialized_agents):
            blockers.append("specialist_voting_violation")
        closed_count = sum(
            1
            for event in cleanup_results
            for result in event.get("results", [])
            if result.get("success") and result.get("closed")
        )
        if closed_count < 2:
            blockers.append("cleanup_incomplete")
        if len(create_request_ids) > 2:
            blockers.append("too_many_agentbay_creates")
        if len(delete_request_ids) < 2:
            blockers.append("missing_cleanup_request_ids")
        success = not blockers

    finished_wall = _utc_now()
    evidence = {
        "phase_date": PHASE_DATE,
        "attempt_id": unique_suffix,
        "started_at": _isoformat(started_wall),
        "finished_at": _isoformat(finished_wall),
        "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        "settings": {
            "provider": settings.provider,
            "model": settings.active_model,
            "subtask_max_attempts": settings.subtask_max_attempts,
            "society_max_model_workers": settings.society_max_model_workers,
            "society_max_agentbay_sessions": settings.society_max_agentbay_sessions,
            "society_max_media_jobs": settings.society_max_media_jobs,
            "agentbay_session_timeout_seconds": settings.agentbay_session_timeout_seconds,
        },
        "preflight": {
            "agentbay_ready": bool(preflight.get("provider_services", {}).get("agentbay", {}).get("ready")),
            "image_ready": bool(preflight.get("provider_services", {}).get("image", {}).get("ready")),
            "video_ready": bool(preflight.get("provider_services", {}).get("video", {}).get("ready")),
            "typed_blocker_count": len(preflight.get("typed_blockers", [])),
        },
        "plan": {
            "assignment_ids": [assignment.id for assignment in plan.assignments],
            "node_ids": [node.id for node in plan.work_graph],
            "tool_grants": {
                assignment.id: [tool_id for grant in assignment.tool_grants for tool_id in grant.tool_ids]
                for assignment in plan.assignments
            },
            "validates_assignment_ids": {
                assignment.id: list(assignment.validates_assignment_ids)
                for assignment in plan.assignments
            },
        },
        "validation": {
            "passed": validation_error is None,
            "issues": [] if validation_error is None else [
                {"code": issue.code, "message": issue.message, "assignment_id": issue.assignment_id, "node_id": issue.node_id}
                for issue in validation_error.issues
            ],
        },
        "live_attempt": {
            "ran": live_result is not None or live_exception is not None,
            "exception": live_exception,
            "terminal_status": live_result.graph_result.terminal_status.value if live_result is not None else None,
            "execution_order": list(live_result.graph_result.execution_order) if live_result is not None else [],
            "max_model_concurrency": live_result.graph_result.max_model_concurrency if live_result is not None else None,
            "max_agentbay_concurrency": live_result.graph_result.max_agentbay_concurrency if live_result is not None else None,
            "materialized_agents": [] if live_result is None else [
                {
                    "id": agent.id,
                    "assignment_id": agent.assignment_id,
                    "template_id": agent.template_id,
                    "can_vote": agent.can_vote,
                }
                for agent in live_result.materialized_agents
            ],
            "nodes": {} if live_result is None else {
                node_id: _summarize_node(record)
                for node_id, record in live_result.graph_result.nodes.items()
            },
            "node_outputs": {} if live_result is None else _sanitize_value(live_result.node_outputs),
        },
        "artifacts": builder_artifacts,
        "events": event_log,
        "agentbay": {
            "create_request_ids": create_request_ids,
            "delete_request_ids": delete_request_ids,
            "cleanup_events": cleanup_results,
        },
        "success": success,
        "blockers": blockers,
        "evidence_path": str(evidence_path),
    }
    _atomic_write_json(evidence_path, evidence)

    builder_status = None if live_result is None else live_result.graph_result.nodes["builder-node"].status.value
    test_status = None if live_result is None else live_result.graph_result.nodes["test-node"].status.value
    builder_attempts = [] if live_result is None else [
        {"status": attempt.status.value, "code": attempt.code, "message": attempt.message}
        for attempt in live_result.graph_result.nodes["builder-node"].attempts
    ]
    test_attempts = [] if live_result is None else [
        {"status": attempt.status.value, "code": attempt.code, "message": attempt.message}
        for attempt in live_result.graph_result.nodes["test-node"].attempts
    ]
    validation_output = {} if live_result is None else _sanitize_value(live_result.node_outputs.get("test-node", {}))
    summary_lines = [
        "Fix: task-bound LLM start_execution_environment wrapper plus mandatory tool-evidence enforcement for composed agents.",
        "Tests: focused non-provider test run recorded in this attempt evidence.",
        f"Live results: builder={builder_status} attempts={json.dumps(builder_attempts)}; test_engineer={test_status} attempts={json.dumps(test_attempts)}.",
        f"Request IDs: create={json.dumps(create_request_ids)} delete={json.dumps(delete_request_ids)}.",
        f"Artifact/validation evidence: builder_artifacts={json.dumps(builder_artifacts)} test_output={json.dumps(validation_output)}.",
        f"Cleanup: cleanup_events={len(cleanup_results)} max_agentbay_sessions={settings.society_max_agentbay_sessions} idle_release={settings.agentbay_session_timeout_seconds}s.",
        f"Blockers: {', '.join(blockers) if blockers else 'none'}",
        f"Evidence: {evidence_path}",
    ]
    summary_text = "\n".join(summary_lines)
    _atomic_write_text(output_last_message_path, summary_text)
    return evidence, summary_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one real composed AgentBay CompositionRuntime benchmark gate.")
    parser.add_argument(
        "--output-last-message-path",
        type=Path,
        default=SUMMARY_PATH_DEFAULT,
        help="Path to the concise summary file written at the end of the run.",
    )
    args = parser.parse_args()
    asyncio.run(_run_once(args.output_last_message_path.resolve()))


if __name__ == "__main__":
    main()
