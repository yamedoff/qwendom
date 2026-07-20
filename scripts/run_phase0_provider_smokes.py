"""Run one credential-gated Phase 0 provider smoke pass and persist local evidence.

This runner is intentionally narrow:
- loads `backend/.env` through `Settings`
- uses the existing AgentBay, image, and video adapters
- performs at most one paid create/generate/submit call per authorized service
- writes one atomic JSON evidence record and one concise summary file

It never prints or serializes credential values.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import time
import types
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_backend_module(module_name: str, module_path: Path) -> Any:
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


backend_pkg = sys.modules.setdefault("backend", types.ModuleType("backend"))
backend_pkg.__path__ = [str(BACKEND_DIR)]
society_pkg = sys.modules.setdefault("backend.society", types.ModuleType("backend.society"))
society_pkg.__path__ = [str(BACKEND_DIR / "society")]
tools_pkg = sys.modules.setdefault("backend.society.tools", types.ModuleType("backend.society.tools"))
tools_pkg.__path__ = [str(BACKEND_DIR / "society" / "tools")]

config_module = _load_backend_module("backend.config", BACKEND_DIR / "config.py")
_load_backend_module("backend.society.capability_registry", BACKEND_DIR / "society" / "capability_registry.py")
_load_backend_module("backend.society.error_taxonomy", BACKEND_DIR / "society" / "error_taxonomy.py")
provider_preflight_module = _load_backend_module(
    "backend.society.provider_preflight",
    BACKEND_DIR / "society" / "provider_preflight.py",
)
agentbay_module = _load_backend_module("backend.society.tools.agentbay", BACKEND_DIR / "society" / "tools" / "agentbay.py")
media_store_module = _load_backend_module(
    "backend.society.tools.media_store",
    BACKEND_DIR / "society" / "tools" / "media_store.py",
)
image_module = _load_backend_module(
    "backend.society.tools.image_generation",
    BACKEND_DIR / "society" / "tools" / "image_generation.py",
)
video_module = _load_backend_module(
    "backend.society.tools.video_generation",
    BACKEND_DIR / "society" / "tools" / "video_generation.py",
)

Settings = config_module.Settings
model_capability_preflight = provider_preflight_module.model_capability_preflight
AgentBayTools = agentbay_module.AgentBayTools
MediaArtifactStore = media_store_module.MediaArtifactStore
IMAGE_MODEL_ID = image_module.IMAGE_MODEL_ID
ImageGenerationTools = image_module.ImageGenerationTools
VIDEO_MODEL_ID = video_module.VIDEO_MODEL_ID
VideoGenerationTools = video_module.VideoGenerationTools


PHASE_DATE = "2026-07-15"
AGENTBAY_PURPOSE = "Phase 0 provider smoke: create one bounded builder sandbox"
AGENTBAY_TASK_ID = "phase0-provider-smoke-agentbay"
AGENTBAY_REMOTE_ARTIFACT = "/tmp/phase0_smoke.txt"
AGENTBAY_ARTIFACT_KIND = "phase0-agentbay"
IMAGE_PROMPT = "Abstract geometric composition with simple shapes and flat colors."
VIDEO_PROMPT = "Abstract geometric animation with simple shapes drifting slowly."
VIDEO_TIMEOUT_SECONDS = 360
VIDEO_POLL_INTERVAL_SECONDS = 10
SUMMARY_PATH_DEFAULT = ROOT / "backend" / "benchmark_results" / "provider-smokes" / PHASE_DATE / "last-message.txt"


@dataclass(frozen=True)
class EvidencePaths:
    evidence_path: Path
    summary_path: Path
    media_store_dir: Path
    agentbay_artifact_dir: Path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sanitize_message(message: Any) -> str | None:
    if not message:
        return None
    text = str(message)
    for token in (
        "Authorization",
        "authorization",
        "Bearer ",
        "api_key",
        "secret",
        "password",
        "workspace",
    ):
        if token in text:
            text = text.replace(token, "[redacted]")
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        text = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return text[:400]


def _sanitize_host(base_url: str) -> str | None:
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    if not parsed.hostname:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _extract_error(payload: dict[str, Any]) -> dict[str, str | None]:
    return {
        "code": payload.get("error_code"),
        "message": _sanitize_message(payload.get("error_message")),
    }


def _extract_request_id(payload: dict[str, Any]) -> str | None:
    request_id = payload.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("request_id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _artifact_evidence(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": manifest.get("artifact_id"),
        "path": manifest.get("local_relative_path"),
        "sha256": manifest.get("sha256"),
        "byte_size": manifest.get("byte_size"),
        "mime_type": manifest.get("mime_type"),
        "duration": manifest.get("duration"),
        "width": manifest.get("width"),
        "height": manifest.get("height"),
        "request_id": manifest.get("request_id"),
        "model_id": manifest.get("model_id"),
    }


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


def _build_paths(summary_path: Path | None) -> EvidencePaths:
    base_dir = ROOT / "backend" / "benchmark_results" / "provider-smokes" / PHASE_DATE
    unique_suffix = f"{datetime.now(UTC).strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
    evidence_path = base_dir / f"phase0-provider-smokes-{unique_suffix}.json"
    resolved_summary = (summary_path or SUMMARY_PATH_DEFAULT).resolve()
    return EvidencePaths(
        evidence_path=evidence_path,
        summary_path=resolved_summary,
        media_store_dir=base_dir / f"media-store-{unique_suffix}",
        agentbay_artifact_dir=base_dir / f"agentbay-artifacts-{unique_suffix}",
    )


def load_settings() -> Settings:
    env_path = BACKEND_DIR / ".env"
    return Settings(_env_file=env_path)


def build_http_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        trust_env=False,
    )


def suppress_provider_logs() -> None:
    """Reduce SDK and HTTP chatter so smoke runs only emit persisted evidence."""

    for logger_name in ("AgentBay", "httpx", "httpcore"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)


def _load_private_video_job(store: MediaArtifactStore, job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    try:
        return store.load_video_job(job_id)
    except Exception:
        return None


def run_agentbay_smoke(settings: Settings, paths: EvidencePaths) -> dict[str, Any]:
    started_at = time.monotonic()
    record: dict[str, Any] = {
        "service": "agentbay",
        "region": settings.agentbay_region_id,
        "host": settings.agentbay_endpoint,
        "image_id": settings.agentbay_image_id,
        "status": "not_started",
        "timing_ms": None,
        "request_ids": {},
        "listing": None,
        "artifact": None,
        "cleanup": {"close": None, "close_all": None},
        "error": None,
    }
    smoke_settings = settings.model_copy(update={"agentbay_session_timeout_seconds": 120})
    tools = AgentBayTools(
        task_id=AGENTBAY_TASK_ID,
        role_key="builder",
        allowed_remote_roots=["/tmp"],
        artifact_root=paths.agentbay_artifact_dir,
        settings=smoke_settings,
    )
    handle: str | None = None
    try:
        start_result = tools.start_execution_environment(AGENTBAY_TASK_ID, AGENTBAY_PURPOSE)
        record["request_ids"]["create"] = _extract_request_id(start_result)
        if not start_result.get("success"):
            record["status"] = "failed"
            record["error"] = _extract_error(start_result)
            return record
        handle = str(start_result["data"]["handle"])

        run_result = tools.run_code(handle, "python", "print('phase0-agentbay-ok')", 30)
        record["request_ids"]["run_code"] = _extract_request_id(run_result)
        if not run_result.get("success"):
            record["status"] = "failed"
            record["error"] = _extract_error(run_result)
            return record

        write_result = tools.write_text_file(handle, AGENTBAY_REMOTE_ARTIFACT, "phase0-agentbay-ok\n", "overwrite")
        record["request_ids"]["write"] = _extract_request_id(write_result)
        if not write_result.get("success"):
            record["status"] = "failed"
            record["error"] = _extract_error(write_result)
            return record

        list_result = tools.list_files(handle, str(Path(AGENTBAY_REMOTE_ARTIFACT).parent).replace("\\", "/"))
        record["request_ids"]["list"] = _extract_request_id(list_result)
        if not list_result.get("success"):
            record["status"] = "failed"
            record["error"] = _extract_error(list_result)
            return record
        record["listing"] = {
            "path": list_result.get("data", {}).get("path"),
            "entries": list_result.get("data", {}).get("entries", []),
        }

        export_result = tools.export_artifact(handle, AGENTBAY_REMOTE_ARTIFACT, AGENTBAY_ARTIFACT_KIND)
        record["request_ids"]["export"] = _extract_request_id(export_result)
        if not export_result.get("success"):
            record["status"] = "failed"
            record["error"] = _extract_error(export_result)
            return record

        artifact_ref = export_result.get("artifact_references", [{}])[0]
        record["artifact"] = {
            "artifact_id": artifact_ref.get("id"),
            "path": artifact_ref.get("path"),
            "sha256": artifact_ref.get("sha256"),
            "byte_size": artifact_ref.get("size_bytes"),
            "kind": artifact_ref.get("type"),
        }
        record["status"] = "succeeded"
        return record
    finally:
        if handle:
            close_result = tools.close_execution_environment(handle)
            record["cleanup"]["close"] = {
                "status": "succeeded" if close_result.get("success") else "failed",
                "request_id": _extract_request_id(close_result),
                "error": None if close_result.get("success") else _extract_error(close_result),
            }
            close_all_results = tools.close_all()
            same_handle = [item for item in close_all_results if item.get("data", {}).get("handle") == handle]
            if same_handle:
                close_all_result = same_handle[0]
                record["cleanup"]["close_all"] = {
                    "status": "succeeded" if close_all_result.get("success") else "failed",
                    "request_id": _extract_request_id(close_all_result),
                    "error": None if close_all_result.get("success") else _extract_error(close_all_result),
                }
            else:
                record["cleanup"]["close_all"] = {"status": "not_found", "request_id": None, "error": None}
        record["timing_ms"] = int((time.monotonic() - started_at) * 1000)


def run_image_smoke(settings: Settings, store: MediaArtifactStore, client: httpx.Client) -> dict[str, Any]:
    started_at = time.monotonic()
    record: dict[str, Any] = {
        "service": "image",
        "host": _sanitize_host(settings.resolved_model_studio_base_url),
        "model_id": IMAGE_MODEL_ID,
        "status": "not_started",
        "timing_ms": None,
        "request_ids": {},
        "artifacts": [],
        "paid_units": {"images": 1},
        "error": None,
    }
    tools = ImageGenerationTools(settings=settings, artifact_store=store, http_client=client)
    result = tools.generate_images(
        prompt=IMAGE_PROMPT,
        negative_prompt="",
        width=512,
        height=512,
        count=1,
        seed=None,
    )
    record["request_ids"]["generate"] = _extract_request_id(result)
    if result.get("success"):
        artifact_ids = list(result.get("data", {}).get("artifact_ids", []))
        manifests = []
        for artifact_id in artifact_ids[:1]:
            inspect_result = tools.inspect_image(str(artifact_id))
            if inspect_result.get("success"):
                manifests.append(_artifact_evidence(inspect_result["data"]["artifact"]))
        record["artifacts"] = manifests
        record["status"] = "succeeded"
    else:
        record["status"] = "failed"
        record["error"] = _extract_error(result)
    record["timing_ms"] = int((time.monotonic() - started_at) * 1000)
    return record


def run_video_smoke(settings: Settings, store: MediaArtifactStore, client: httpx.Client) -> dict[str, Any]:
    started_at = time.monotonic()
    record: dict[str, Any] = {
        "service": "video",
        "host": _sanitize_host(settings.resolved_model_studio_base_url),
        "model_id": VIDEO_MODEL_ID,
        "status": "not_started",
        "timing_ms": None,
        "request_ids": {},
        "job": None,
        "artifact": None,
        "paid_units": {"video_seconds": 5},
        "error": None,
    }
    tools = VideoGenerationTools(settings=settings, artifact_store=store, http_client=client)
    submit_result = tools.submit_text_to_video(
        prompt=VIDEO_PROMPT,
        negative_prompt="",
        duration=5,
        size="1280*720",
        seed=None,
    )
    record["request_ids"]["submit"] = _extract_request_id(submit_result)
    if not submit_result.get("success"):
        record["status"] = "failed"
        record["error"] = _extract_error(submit_result)
        record["timing_ms"] = int((time.monotonic() - started_at) * 1000)
        return record

    job = deepcopy(submit_result.get("data", {}).get("job", {}))
    record["job"] = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "duration": job.get("duration"),
        "size": job.get("size"),
        "resolution": job.get("resolution"),
        "ratio": job.get("ratio"),
        "attempts": job.get("attempts"),
        "request_id": record["request_ids"]["submit"],
        "collected_artifact_id": job.get("collected_artifact_id"),
    }
    private_job = _load_private_video_job(store, str(job.get("job_id") or ""))
    if private_job and private_job.get("request_id"):
        record["request_ids"]["submit"] = private_job.get("request_id")
        record["job"]["request_id"] = private_job.get("request_id")

    deadline = time.monotonic() + VIDEO_TIMEOUT_SECONDS
    job_id = str(job["job_id"])
    while time.monotonic() < deadline:
        current_status = str(record["job"]["status"])
        if current_status in {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
            break
        time.sleep(VIDEO_POLL_INTERVAL_SECONDS)
        poll_result = tools.get_video_job(job_id)
        latest_request_id = _extract_request_id(poll_result)
        if latest_request_id:
            record["request_ids"]["poll"] = latest_request_id
        latest_job = deepcopy(poll_result.get("data", {}).get("job", {}))
        if latest_job:
            private_job = _load_private_video_job(store, job_id)
            record["job"] = {
                "job_id": latest_job.get("job_id"),
                "status": latest_job.get("status"),
                "duration": latest_job.get("duration"),
                "size": latest_job.get("size"),
                "resolution": latest_job.get("resolution"),
                "ratio": latest_job.get("ratio"),
                "attempts": latest_job.get("attempts"),
                "request_id": (private_job or {}).get("request_id") or latest_request_id or record["request_ids"].get("submit"),
                "collected_artifact_id": latest_job.get("collected_artifact_id"),
            }
        if not poll_result.get("success") and latest_job.get("status") not in {"FAILED", "UNKNOWN", "CANCELED"}:
            record["status"] = "failed"
            record["error"] = _extract_error(poll_result)
            record["timing_ms"] = int((time.monotonic() - started_at) * 1000)
            return record

    final_status = str(record["job"]["status"])
    if final_status == "SUCCEEDED":
        collect_result = tools.collect_video(job_id)
        record["request_ids"]["collect"] = _extract_request_id(collect_result)
        if collect_result.get("success"):
            record["artifact"] = _artifact_evidence(collect_result["data"]["artifact"])
            record["job"]["collected_artifact_id"] = collect_result["data"]["job"].get("collected_artifact_id")
            record["request_ids"]["collect"] = collect_result["data"]["artifact"].get("request_id")
            record["status"] = "succeeded"
        else:
            record["status"] = "failed"
            record["error"] = _extract_error(collect_result)
    elif final_status in {"FAILED", "CANCELED", "UNKNOWN"}:
        record["status"] = "failed" if final_status != "UNKNOWN" else "pending"
        private_job = store.load_video_job(job_id)
        record["error"] = {
            "code": private_job.get("error_code"),
            "message": _sanitize_message(private_job.get("error_message")),
        }
    else:
        record["status"] = "pending"
    record["timing_ms"] = int((time.monotonic() - started_at) * 1000)
    return record


def summarize(record: dict[str, Any]) -> str:
    lines = []
    for service_name in ("agentbay", "image", "video"):
        service = record["services"][service_name]
        request_ids = ", ".join(
            f"{name}={value}" for name, value in service.get("request_ids", {}).items() if value
        ) or "none"
        artifact_bits: list[str] = []
        if service_name == "agentbay" and service.get("artifact"):
            artifact = service["artifact"]
            artifact_bits.append(
                f"{artifact.get('artifact_id')} {artifact.get('path')} sha256={artifact.get('sha256')} size={artifact.get('byte_size')}"
            )
        if service_name == "image":
            for artifact in service.get("artifacts", []):
                artifact_bits.append(
                    f"{artifact.get('artifact_id')} {artifact.get('path')} sha256={artifact.get('sha256')} size={artifact.get('byte_size')}"
                )
        if service_name == "video" and service.get("artifact"):
            artifact = service["artifact"]
            artifact_bits.append(
                f"{artifact.get('artifact_id')} {artifact.get('path')} sha256={artifact.get('sha256')} size={artifact.get('byte_size')}"
            )
        artifacts = "; ".join(artifact_bits) or "none"
        cleanup = service.get("cleanup")
        cleanup_text = ""
        if cleanup:
            cleanup_text = f" cleanup={json.dumps(cleanup, sort_keys=True)}"
        blocker = service.get("error")
        blocker_text = ""
        if blocker and (blocker.get("code") or blocker.get("message")):
            blocker_text = f" blocker={json.dumps(blocker, sort_keys=True)}"
        lines.append(
            f"{service_name}: status={service.get('status')} request_ids={request_ids} artifacts={artifacts}{cleanup_text}{blocker_text}"
        )
    preflight = record["preflight"]
    blocker_codes = ",".join(preflight["blocker_codes"]) or "none"
    lines.append(
        "preflight: "
        f"agentbay_ready={preflight['agentbay_ready']} "
        f"image_ready={preflight['image_ready']} "
        f"video_ready={preflight['video_ready']} "
        f"roadmap_ready={preflight['roadmap_ready']} "
        f"blockers={blocker_codes}"
    )
    lines.append("estimated_paid_units: image=1 video_seconds=5")
    return "\n".join(lines)


def run(
    summary_path: Path | None = None,
    *,
    agentbay_only: bool = False,
    allow_media_after_agentbay_failure: bool = False,
) -> EvidencePaths:
    suppress_provider_logs()
    settings = load_settings()
    paths = _build_paths(summary_path)
    preflight = model_capability_preflight(settings)
    record: dict[str, Any] = {
        "timestamp": _isoformat(_utc_now()),
        "phase": "provider_smokes_phase0",
        "services": {
            "agentbay": {
                "service": "agentbay",
                "status": "skipped",
                "region": settings.agentbay_region_id,
                "host": settings.agentbay_endpoint,
                "image_id": settings.agentbay_image_id,
                "request_ids": {},
                "artifact": None,
                "cleanup": None,
                "timing_ms": None,
                "error": None,
            },
            "image": {
                "service": "image",
                "status": "skipped",
                "host": _sanitize_host(settings.resolved_model_studio_base_url),
                "model_id": IMAGE_MODEL_ID,
                "request_ids": {},
                "artifacts": [],
                "timing_ms": None,
                "paid_units": {"images": 1},
                "error": None,
            },
            "video": {
                "service": "video",
                "status": "skipped",
                "host": _sanitize_host(settings.resolved_model_studio_base_url),
                "model_id": VIDEO_MODEL_ID,
                "request_ids": {},
                "job": None,
                "artifact": None,
                "timing_ms": None,
                "paid_units": {"video_seconds": 5},
                "error": None,
            },
        },
        "preflight": {
            "roadmap_ready": bool(preflight.get("roadmap_ready")),
            "agentbay_ready": bool(preflight["provider_services"]["agentbay"]["ready"]),
            "image_ready": bool(preflight["provider_services"]["image"]["ready"]),
            "video_ready": bool(preflight["provider_services"]["video"]["ready"]),
            "blocker_codes": [item["code"] for item in preflight.get("typed_blockers", [])],
        },
        "development_only": {
            "allow_media_after_agentbay_failure": bool(allow_media_after_agentbay_failure),
            "non_official": bool(allow_media_after_agentbay_failure),
            "non_comparable": bool(allow_media_after_agentbay_failure),
        },
    }

    isolated_agentbay_failure = False
    if record["preflight"]["agentbay_ready"]:
        record["services"]["agentbay"] = run_agentbay_smoke(settings, paths)
        isolated_agentbay_failure = record["services"]["agentbay"]["status"] == "failed"
    else:
        record["services"]["agentbay"]["error"] = {
            "code": "preflight_agentbay_blocked",
            "message": "Static preflight blocked AgentBay smoke.",
        }

    if agentbay_only:
        record["services"]["image"]["error"] = {
            "code": "agentbay_only_mode",
            "message": "Image smoke disabled by --agentbay-only.",
        }
        record["services"]["video"]["error"] = {
            "code": "agentbay_only_mode",
            "message": "Video smoke disabled by --agentbay-only.",
        }
    else:
        client = build_http_client()
        try:
            store = MediaArtifactStore(paths.media_store_dir)
            can_run_media = record["preflight"]["image_ready"] and record["preflight"]["video_ready"]
            can_continue_after_agentbay_failure = isolated_agentbay_failure and allow_media_after_agentbay_failure
            if can_run_media and (record["services"]["agentbay"]["status"] == "succeeded" or can_continue_after_agentbay_failure):
                record["services"]["image"] = run_image_smoke(settings, store, client)
                if record["services"]["image"]["status"] == "succeeded":
                    record["services"]["video"] = run_video_smoke(settings, store, client)
                else:
                    record["services"]["video"]["error"] = {
                        "code": "image_smoke_failed",
                        "message": "Video smoke skipped because the image smoke failed after its single paid attempt.",
                    }
            else:
                if not record["preflight"]["image_ready"]:
                    record["services"]["image"]["error"] = {
                        "code": "preflight_image_blocked",
                        "message": "Static preflight blocked image smoke.",
                    }
                elif isolated_agentbay_failure:
                    record["services"]["image"]["error"] = {
                        "code": "agentbay_smoke_failed",
                        "message": (
                            "Image smoke skipped because the AgentBay smoke failed. "
                            "Use --allow-media-after-agentbay-failure only for development-only non-comparable diagnostics."
                        ),
                    }
                if not record["preflight"]["video_ready"]:
                    record["services"]["video"]["error"] = {
                        "code": "preflight_video_blocked",
                        "message": "Static preflight blocked video smoke.",
                    }
                elif isolated_agentbay_failure:
                    record["services"]["video"]["error"] = {
                        "code": "agentbay_smoke_failed",
                        "message": (
                            "Video smoke skipped because the AgentBay smoke failed. "
                            "Use --allow-media-after-agentbay-failure only for development-only non-comparable diagnostics."
                        ),
                    }
        finally:
            client.close()

    _atomic_write_json(paths.evidence_path, record)
    _atomic_write_text(paths.summary_path, summarize(record))
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-last-message-path",
        type=Path,
        default=SUMMARY_PATH_DEFAULT,
        help="Path for the concise final summary file.",
    )
    parser.add_argument(
        "--agentbay-only",
        action="store_true",
        help="Run only the AgentBay smoke and skip image/video calls.",
    )
    parser.add_argument(
        "--allow-media-after-agentbay-failure",
        action="store_true",
        help="Development-only diagnostic opt-in that allows image/video to continue after an AgentBay smoke failure; marks the record non-official and non-comparable.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run(
        args.output_last_message_path,
        agentbay_only=args.agentbay_only,
        allow_media_after_agentbay_failure=args.allow_media_after_agentbay_failure,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
