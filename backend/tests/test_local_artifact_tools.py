from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.tools.local_artifacts import LocalArtifactTools, MAX_INSPECT_BYTES


def make_tools(tmp_path: Path, *, role_key: str = "test_engineer", event_sink=None) -> LocalArtifactTools:
    return LocalArtifactTools(role_key=role_key, artifact_root=tmp_path, event_sink=event_sink)


def test_inspect_artifact_returns_bounded_text_metadata_and_content(tmp_path: Path) -> None:
    artifact = tmp_path / "agentbay" / "artifact.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    result = make_tools(tmp_path).inspect_artifact(str(artifact))

    assert result["success"] is True
    assert result["safe_relative_path"] == "agentbay/artifact.txt"
    assert result["is_text"] is True
    assert "def add" in result["content"]
    assert len(result["sha256"]) == 64


def test_inspect_artifact_rejects_path_escape_and_missing_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    escaped = make_tools(tmp_path).inspect_artifact(str(outside))
    missing = make_tools(tmp_path).inspect_artifact("missing.txt")

    assert escaped["success"] is False
    assert missing["success"] is False
    assert escaped["error_code"] == "artifact_inspection_failed"
    assert missing["error_code"] == "artifact_inspection_failed"


def test_inspect_artifact_rejects_directories_and_oversized_files(tmp_path: Path) -> None:
    folder = tmp_path / "dir"
    folder.mkdir()
    oversized = tmp_path / "big.bin"
    oversized.write_bytes(b"x" * (MAX_INSPECT_BYTES + 1))

    folder_result = make_tools(tmp_path).inspect_artifact("dir")
    oversized_result = make_tools(tmp_path).inspect_artifact("big.bin")

    assert folder_result["success"] is False
    assert oversized_result["success"] is False


def test_inspect_artifact_rejects_symlink_paths(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("hello", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this machine")

    result = make_tools(tmp_path).inspect_artifact("link.txt")
    assert result["success"] is False


def test_non_text_artifact_returns_metadata_without_content(tmp_path: Path) -> None:
    artifact = tmp_path / "payload.bin"
    artifact.write_bytes(b"\xff\x00\x01")

    result = make_tools(tmp_path).inspect_artifact("payload.bin")

    assert result["success"] is True
    assert result["is_text"] is False
    assert result["content"] is None


def test_report_independent_validation_is_bounded_and_side_effect_free(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    result = make_tools(tmp_path, event_sink=events.append).report_independent_validation(
        passed=True,
        checks=["artifact checksum matches", "sandbox assertion passed"],
        failures=[],
        inspected_artifact_refs=["agentbay/exported_calc.py"],
        summary="Independent validation passed.",
    )

    assert result["passed"] is True
    assert result["checks"] == ["artifact checksum matches", "sandbox assertion passed"]
    assert result["inspected_artifact_refs"] == ["agentbay/exported_calc.py"]
    assert events[0]["event_type"] == "local_independent_validation_reported"


def test_report_independent_validation_accepts_json_object_evidence(tmp_path: Path) -> None:
    result = make_tools(tmp_path).report_independent_validation(
        passed=False,
        checks={"compile": "PASS", "behavior": "FAIL"},
        failures={"behavior": "expected support scope"},
        inspected_artifact_refs={"auth": "agentbay/producer/auth.py"},
        summary="One behavioral failure.",
    )

    assert result["checks"] == ["compile: PASS", "behavior: FAIL"]
    assert result["failures"] == ["behavior: expected support scope"]
    assert result["inspected_artifact_refs"] == ["auth: agentbay/producer/auth.py"]


def test_report_independent_validation_decodes_json_string_arrays(tmp_path: Path) -> None:
    result = make_tools(tmp_path).report_independent_validation(
        passed=True,
        checks='["compile: PASS", "behavior: PASS"]',
        failures="[]",
        inspected_artifact_refs='["agentbay/producer/auth.py"]',
        summary="Validated.",
    )

    assert result["checks"] == ["compile: PASS", "behavior: PASS"]
    assert result["failures"] == []
    assert result["inspected_artifact_refs"] == ["agentbay/producer/auth.py"]


def test_report_independent_validation_normalizes_failed_checks_alias(tmp_path: Path) -> None:
    result = make_tools(tmp_path).report_independent_validation(
        passed=False,
        checks=["behavior checked"],
        failed_checks='["behavior mismatch"]',
        inspected_artifact_refs=["agentbay/producer/auth.py"],
        summary="Failed.",
    )

    assert result["failures"] == ["behavior mismatch"]


def test_inspect_artifact_failure_emits_event(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []

    result = make_tools(tmp_path, event_sink=events.append).inspect_artifact("missing.txt")

    assert result["success"] is False
    assert events[0]["event_type"] == "local_artifact_inspection_failed"


def test_unauthorized_role_cannot_construct_local_artifact_tools(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        make_tools(tmp_path, role_key="builder")
