from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_phase0_provider_smokes import (
    _atomic_write_json,
    _sanitize_host,
    _sanitize_message,
    parse_args,
    run,
    run_agentbay_smoke,
    summarize,
)


def test_sanitize_message_removes_url_query_and_truncates() -> None:
    message = "provider failed at https://dashscope-intl.aliyuncs.com/api/v1/tasks/x?token=secret"
    sanitized = _sanitize_message(message)
    assert sanitized is not None
    assert "?token=secret" not in sanitized
    assert "dashscope-intl.aliyuncs.com" in sanitized


def test_sanitize_host_keeps_only_scheme_host_and_path() -> None:
    assert _sanitize_host("https://dashscope-intl.aliyuncs.com/api/v1?x=1") == (
        "https://dashscope-intl.aliyuncs.com/api/v1"
    )


def test_atomic_json_write_and_summary_shape(tmp_path: Path) -> None:
    payload = {"b": 2, "a": 1}
    output = tmp_path / "evidence.json"
    _atomic_write_json(output, payload)
    assert json.loads(output.read_text(encoding="utf-8")) == payload

    summary = summarize(
        {
            "services": {
                "agentbay": {
                    "status": "succeeded",
                    "request_ids": {"create": "req-a"},
                    "artifact": {
                        "artifact_id": "artifact-a",
                        "path": "agentbay/file.txt",
                        "sha256": "abc",
                        "byte_size": 10,
                    },
                    "cleanup": {"close": {"status": "succeeded"}},
                    "error": None,
                },
                "image": {
                    "status": "succeeded",
                    "request_ids": {"generate": "req-b"},
                    "artifacts": [
                        {
                            "artifact_id": "artifact-b",
                            "path": "artifacts/img.png",
                            "sha256": "def",
                            "byte_size": 20,
                        }
                    ],
                    "error": None,
                },
                "video": {
                    "status": "pending",
                    "request_ids": {"submit": "req-c"},
                    "artifact": None,
                    "error": {"code": "provider_pending", "message": "still running"},
                },
            },
            "preflight": {
                "agentbay_ready": True,
                "image_ready": True,
                "video_ready": True,
                "roadmap_ready": True,
                "blocker_codes": [],
            },
        }
    )
    assert "agentbay: status=succeeded" in summary
    assert "image: status=succeeded" in summary
    assert "video: status=pending" in summary
    assert "blockers=none" in summary


def test_parse_args_supports_agentbay_only(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_phase0_provider_smokes.py", "--agentbay-only"])
    args = parse_args()
    assert args.agentbay_only is True


def test_parse_args_supports_development_only_media_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_phase0_provider_smokes.py", "--allow-media-after-agentbay-failure"],
    )
    args = parse_args()
    assert args.allow_media_after_agentbay_failure is True


def test_run_agentbay_smoke_records_list_before_export(tmp_path: Path, monkeypatch) -> None:
    class FakeTools:
        def __init__(self, **kwargs):
            self.calls = []

        def start_execution_environment(self, task_id, purpose):
            self.calls.append(("start", task_id, purpose))
            return {"success": True, "data": {"handle": "opaque"}, "request_id": "req-create"}

        def run_code(self, handle, language, code, timeout_seconds):
            self.calls.append(("run_code", handle, language, code, timeout_seconds))
            return {"success": True, "request_id": "req-run"}

        def write_text_file(self, handle, path, content, mode):
            self.calls.append(("write", handle, path, content, mode))
            return {"success": True, "request_id": "req-write"}

        def list_files(self, handle, path):
            self.calls.append(("list", handle, path))
            return {
                "success": True,
                "request_id": "req-list",
                "data": {"path": path, "entries": [{"path": "/tmp/phase0_smoke.txt", "type": "file"}]},
            }

        def export_artifact(self, handle, path, artifact_kind):
            self.calls.append(("export", handle, path, artifact_kind))
            return {
                "success": True,
                "request_id": "req-export",
                "artifact_references": [
                    {
                        "id": "artifact-1",
                        "path": str(tmp_path / "phase0_smoke.txt"),
                        "sha256": "abc",
                        "size_bytes": 18,
                        "type": "phase0-agentbay",
                    }
                ],
            }

        def close_execution_environment(self, handle):
            self.calls.append(("close", handle))
            return {"success": True, "request_id": "req-close"}

        def close_all(self):
            self.calls.append(("close_all",))
            return [{"success": True, "request_id": "req-close-all", "data": {"handle": "opaque"}}]

    created: list[FakeTools] = []

    def fake_factory(**kwargs):
        tool = FakeTools(**kwargs)
        created.append(tool)
        return tool

    monkeypatch.setattr("scripts.run_phase0_provider_smokes.AgentBayTools", fake_factory)
    settings = type(
        "SettingsStub",
        (),
        {
            "agentbay_region_id": "region-x",
            "agentbay_endpoint": "example.test",
            "agentbay_image_id": "code_latest",
            "model_copy": lambda self, update: self,
        },
    )()
    paths = type("PathsStub", (), {"agentbay_artifact_dir": tmp_path / "artifacts"})()

    record = run_agentbay_smoke(settings, paths)

    assert record["status"] == "succeeded"
    assert record["request_ids"]["list"] == "req-list"
    assert record["listing"]["entries"] == [{"path": "/tmp/phase0_smoke.txt", "type": "file"}]
    assert [call[0] for call in created[0].calls] == ["start", "run_code", "write", "list", "export", "close", "close_all"]


def test_run_fails_closed_after_agentbay_failure_by_default(tmp_path: Path, monkeypatch) -> None:
    class SettingsStub:
        agentbay_region_id = "region-x"
        agentbay_endpoint = "example.test"
        agentbay_image_id = "code_latest"
        resolved_model_studio_base_url = "https://model.example/v1"

    monkeypatch.setattr("scripts.run_phase0_provider_smokes.load_settings", lambda: SettingsStub())
    monkeypatch.setattr(
        "scripts.run_phase0_provider_smokes.model_capability_preflight",
        lambda settings: {
            "roadmap_ready": True,
            "provider_services": {
                "agentbay": {"ready": True},
                "image": {"ready": True},
                "video": {"ready": True},
            },
            "typed_blockers": [],
        },
    )
    monkeypatch.setattr(
        "scripts.run_phase0_provider_smokes._build_paths",
        lambda summary_path: type(
            "PathsStub",
            (),
            {
                "evidence_path": tmp_path / "evidence.json",
                "summary_path": tmp_path / "summary.txt",
                "media_store_dir": tmp_path / "media-store",
                "agentbay_artifact_dir": tmp_path / "agentbay-artifacts",
            },
        )(),
    )
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.run_agentbay_smoke", lambda settings, paths: {"status": "failed", "request_ids": {}, "artifact": None, "cleanup": None, "error": {"code": "boom", "message": "failed"}})
    image_calls = {"count": 0}
    video_calls = {"count": 0}
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.run_image_smoke", lambda settings, store, client: image_calls.__setitem__("count", image_calls["count"] + 1))
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.run_video_smoke", lambda settings, store, client: video_calls.__setitem__("count", video_calls["count"] + 1))

    run(tmp_path / "summary.txt")

    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert image_calls["count"] == 0
    assert video_calls["count"] == 0
    assert evidence["services"]["image"]["error"]["code"] == "agentbay_smoke_failed"
    assert evidence["services"]["video"]["error"]["code"] == "agentbay_smoke_failed"
    assert evidence["development_only"]["non_official"] is False


def test_run_allows_media_after_agentbay_failure_only_with_opt_in(tmp_path: Path, monkeypatch) -> None:
    class SettingsStub:
        agentbay_region_id = "region-x"
        agentbay_endpoint = "example.test"
        agentbay_image_id = "code_latest"
        resolved_model_studio_base_url = "https://model.example/v1"

    monkeypatch.setattr("scripts.run_phase0_provider_smokes.load_settings", lambda: SettingsStub())
    monkeypatch.setattr(
        "scripts.run_phase0_provider_smokes.model_capability_preflight",
        lambda settings: {
            "roadmap_ready": True,
            "provider_services": {
                "agentbay": {"ready": True},
                "image": {"ready": True},
                "video": {"ready": True},
            },
            "typed_blockers": [],
        },
    )
    monkeypatch.setattr(
        "scripts.run_phase0_provider_smokes._build_paths",
        lambda summary_path: type(
            "PathsStub",
            (),
            {
                "evidence_path": tmp_path / "evidence.json",
                "summary_path": tmp_path / "summary.txt",
                "media_store_dir": tmp_path / "media-store",
                "agentbay_artifact_dir": tmp_path / "agentbay-artifacts",
            },
        )(),
    )
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.run_agentbay_smoke", lambda settings, paths: {"status": "failed", "request_ids": {}, "artifact": None, "cleanup": None, "error": {"code": "boom", "message": "failed"}})
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.build_http_client", lambda: type("ClientStub", (), {"close": lambda self: None})())
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.MediaArtifactStore", lambda path: object())
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.run_image_smoke", lambda settings, store, client: {"status": "succeeded", "request_ids": {}, "artifacts": [], "error": None})
    monkeypatch.setattr("scripts.run_phase0_provider_smokes.run_video_smoke", lambda settings, store, client: {"status": "pending", "request_ids": {}, "artifact": None, "job": None, "error": None})

    run(tmp_path / "summary.txt", allow_media_after_agentbay_failure=True)

    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["services"]["image"]["status"] == "succeeded"
    assert evidence["development_only"]["non_official"] is True
    assert evidence["development_only"]["non_comparable"] is True
