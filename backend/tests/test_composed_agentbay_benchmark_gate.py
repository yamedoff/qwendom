from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import Settings
from scripts import run_composed_agentbay_benchmark_gate as gate
from society.work_graph import (
    NodeExecutionError,
    WorkGraphExecutionResult,
    WorkGraphTerminalStatus,
    WorkNodeAttemptRecord,
    WorkNodeRuntimeRecord,
    WorkNodeStatus,
)


def _settings() -> Settings:
    return Settings(
        LLM_PROVIDER="qwen",
        QWEN_API_KEY="sk-test",
        QWEN_MODEL="qwen3.7-plus",
        DASHSCOPE_API_KEY="dashscope",
        AGENTBAY_API_KEY="agentbay",
        MODEL_STUDIO_WORKSPACE_ID="workspace",
    )


def test_event_record_retains_agentbay_failure_error() -> None:
    record = gate._event_record("agentbay_start_failed", {"assignment_id": "a", "node_id": "n", "error": "Permission denied"})

    assert record["event_type"] == "agentbay_start_failed"
    assert record["error"] == "Permission denied"


def test_event_record_persists_validation_passed_and_failures() -> None:
    record = gate._event_record(
        "local_independent_validation_reported",
        {"assignment_id": "a", "node_id": "n", "passed": False, "checks": ["ok"], "failures": ["missing edge case"]},
    )

    assert record["event_type"] == "local_independent_validation_reported"
    assert record["passed"] is False
    assert record["failures"] == ["missing edge case"]


def test_run_once_retains_typed_node_failures_and_does_not_claim_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "BACKEND_DIR", tmp_path / "backend")
    monkeypatch.setattr(gate, "EVIDENCE_BASE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(gate, "load_settings", _settings)
    monkeypatch.setattr(
        gate,
        "model_capability_preflight",
        lambda _settings: {
            "provider_services": {"agentbay": {"ready": True}, "image": {"ready": False}, "video": {"ready": False}},
            "typed_blockers": [],
        },
    )
    monkeypatch.setattr(gate, "validate_team_composition_plan", lambda *args, **kwargs: None)

    class FakeRuntime:
        def __init__(self, settings, artifact_root, event_sink=None):
            self._event_sink = event_sink

        def available_tool_ids(self):
            return SimpleNamespace(tool_ids=["start_execution_environment", "execute_command", "export_artifact", "inspect_artifact", "report_independent_validation"])

        async def execute_plan(self, *args, **kwargs):
            if self._event_sink is not None:
                self._event_sink("agentbay_start_failed", {"assignment_id": "builder-assignment", "node_id": "builder-node", "error": "Task mismatch"})
            graph = WorkGraphExecutionResult(
                terminal_status=WorkGraphTerminalStatus.FAILED,
                started_at=1.0,
                finished_at=2.0,
                duration_seconds=1.0,
                execution_order=["builder-node"],
                nodes={
                    "builder-node": WorkNodeRuntimeRecord(
                        node_id="builder-node",
                        assignment_id="builder-assignment",
                        status=WorkNodeStatus.FAILED,
                        attempts=[
                            WorkNodeAttemptRecord(
                                attempt=1,
                                status=WorkNodeStatus.FAILED,
                                category="capability",
                                code="mandatory_tool_evidence_missing",
                                message="Mandatory tool evidence missing: ['start_execution_environment']",
                                started_at=1.0,
                                finished_at=2.0,
                                duration_seconds=1.0,
                            )
                        ],
                    ),
                    "test-node": WorkNodeRuntimeRecord(
                        node_id="test-node",
                        assignment_id="test-assignment",
                        status=WorkNodeStatus.BLOCKED,
                        blocked_dependency_ids=["builder-node"],
                    ),
                },
            )
            return SimpleNamespace(
                graph_result=graph,
                materialized_agents=[],
                node_outputs={},
                node_artifact_refs={},
            )

    monkeypatch.setattr(gate, "CompositionRuntime", FakeRuntime)

    evidence, summary = asyncio.run(gate._run_once((tmp_path / "last-message.txt").resolve()))

    assert evidence["success"] is False
    assert "graph_not_completed" in evidence["blockers"]
    assert evidence["live_attempt"]["nodes"]["builder-node"]["attempts"][0]["code"] == "mandatory_tool_evidence_missing"
    assert any(event["event_type"] == "agentbay_start_failed" and event.get("error") == "Task mismatch" for event in evidence["events"])
    assert "mandatory_tool_evidence_missing" in summary
