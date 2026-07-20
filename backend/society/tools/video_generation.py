from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

import httpx
from agno.tools import Toolkit

from config import Settings
from .media_store import MediaArtifactStore, StaleJobVersionError, _isoformat, validate_model_studio_base_url


VIDEO_MODEL_ID = "wan2.7-t2v-2026-06-12"
MAX_PROMPT_LENGTH = 4000
MAX_NEGATIVE_PROMPT_LENGTH = 2000
MAX_SEED = 2_147_483_647
VIDEO_SIZE_MAP = {
    "1280*720": ("720P", "16:9"),
    "720*1280": ("720P", "9:16"),
    "960*960": ("720P", "1:1"),
}
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"})


class VideoGenerationTools(Toolkit):
    """Agno toolkit for bounded video submission, polling, and durable collection."""

    def __init__(
        self,
        *,
        settings: Settings,
        artifact_store: MediaArtifactStore,
        http_client: httpx.Client,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(name="video_generation_tools", auto_register=False)
        self._settings = settings
        self._artifact_store = artifact_store
        self._http_client = http_client
        self._event_sink = event_sink
        self._clock = clock or time.time
        for name in ("submit_text_to_video", "get_video_job", "cancel_video_job", "collect_video", "inspect_video"):
            self.register(getattr(self, name))

    def _success(self, *, data: Mapping[str, Any], error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]:
        payload = {
            "success": error_code is None,
            "data": self._artifact_store.redact_value(dict(data)),
            "error_code": error_code,
            "error_message": self._artifact_store.redact_value(error_message) if error_message else None,
        }
        if self._event_sink is not None:
            self._event_sink(self._artifact_store.redact_value(payload))
        return payload

    def _validate_config(self) -> str | None:
        if not self._settings.media_api_key:
            return "Missing DASHSCOPE_API_KEY or QWEN_API_KEY"
        if self._settings.wan_video_model != VIDEO_MODEL_ID:
            return "Configured video model does not match the frozen Phase 3 model"
        try:
            validate_model_studio_base_url(self._settings.resolved_model_studio_base_url)
        except ValueError:
            return "Media service base URL configuration is invalid"
        return None

    def _validate_prompt(self, value: str, *, allow_empty: bool, field_name: str, limit: int) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        trimmed = value.strip()
        if not trimmed and not allow_empty:
            raise ValueError(f"{field_name} must not be empty")
        if len(trimmed) > limit:
            raise ValueError(f"{field_name} exceeds maximum length")
        return trimmed

    def _load_job(self, job_id: str) -> dict[str, Any]:
        return self._artifact_store.load_video_job(job_id)

    def submit_text_to_video(
        self,
        prompt: str,
        negative_prompt: str = "",
        duration: int = 5,
        size: str = "1280*720",
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Submit an asynchronous text-to-video job and persist a private local job record."""

        config_error = self._validate_config()
        if config_error is not None:
            return self._success(data={}, error_code="config_video_unavailable", error_message=config_error)
        try:
            validated_prompt = self._validate_prompt(prompt, allow_empty=False, field_name="prompt", limit=MAX_PROMPT_LENGTH)
            validated_negative = self._validate_prompt(
                negative_prompt,
                allow_empty=True,
                field_name="negative_prompt",
                limit=MAX_NEGATIVE_PROMPT_LENGTH,
            )
            duration = int(duration)
            if duration not in {5, 10, 15}:
                raise ValueError("duration must be one of 5, 10, or 15")
            if size not in VIDEO_SIZE_MAP:
                raise ValueError("size is not supported by the local resolution mapping")
            if seed is not None and (int(seed) < 0 or int(seed) > MAX_SEED):
                raise ValueError("seed must be between 0 and 2147483647")
        except Exception as exc:
            return self._success(data={}, error_code="validation_video_request", error_message=str(exc))

        resolution, ratio = VIDEO_SIZE_MAP[size]
        body = {
            "model": VIDEO_MODEL_ID,
            "input": {
                "prompt": validated_prompt,
                "negative_prompt": validated_negative,
            },
            "parameters": {
                "resolution": resolution,
                "ratio": ratio,
                "prompt_extend": True,
                "watermark": False,
                "duration": duration,
            },
        }
        if seed is not None:
            body["parameters"]["seed"] = int(seed)
        base_url = validate_model_studio_base_url(self._settings.resolved_model_studio_base_url)
        try:
            response = self._http_client.post(
                f"{base_url}/services/aigc/video-generation/video-synthesis",
                headers={
                    "Authorization": f"Bearer {self._settings.media_api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                },
                json=body,
                timeout=60.0,
            )
            response.raise_for_status()
            payload = response.json()
            provider_task_id = str(payload.get("output", {}).get("task_id") or "")
            status = str(payload.get("output", {}).get("task_status") or "UNKNOWN")
            request_id = str(payload.get("request_id") or "")
            if not provider_task_id:
                raise ValueError("Provider did not return a task identifier")
        except httpx.TimeoutException:
            return self._success(data={}, error_code="network_video_timeout", error_message="Video submit timed out")
        except Exception as exc:
            return self._success(data={}, error_code="provider_video_submit", error_message=str(exc))

        record = self._artifact_store.create_video_job(
            job_id=None,
            model_id=VIDEO_MODEL_ID,
            prompt=validated_prompt,
            negative_prompt=validated_negative,
            request_id=request_id,
            provider_task_id=provider_task_id,
            duration=duration,
            size=size,
            resolution=resolution,
            ratio=ratio,
            seed=seed,
            initial_status=status,
        )
        return self._success(data={"job": self._artifact_store.public_job_view(record)})

    def get_video_job(self, job_id: str) -> dict[str, Any]:
        """Load a local video job and perform at most one due provider poll."""

        try:
            with self._artifact_store.job_transaction(job_id):
                record = self._load_job(job_id)
                now = self._clock()
                next_poll_at = record.get("next_poll_at")
                if record.get("cancellation_requested") or record.get("status") in TERMINAL_STATUSES:
                    return self._success(data={"job": self._artifact_store.public_job_view(record)})
                now_dt = datetime.fromtimestamp(now, UTC)
                if record.get("task_expires_at") and next_poll_at and _isoformat(now_dt) > record.get("task_expires_at"):
                    record["status"] = "UNKNOWN"
                    record["error_code"] = "provider_video_task_expired"
                    record["error_message"] = "The provider task expired before completion"
                    record["completed_at"] = _isoformat(now_dt)
                    record = self._artifact_store.save_video_job(job_id, record, expected_version=int(record.get("version", 0)))
                    return self._success(data={"job": self._artifact_store.public_job_view(record)})
                due = next_poll_at is None or _isoformat(now_dt) >= str(next_poll_at)
                if not due:
                    return self._success(data={"job": self._artifact_store.public_job_view(record)})
                expected_version = int(record.get("version", 0))
                provider_task_id = str(record["provider_task_id"])
        except Exception as exc:
            return self._success(data={"job_id": job_id}, error_code="state_video_job_not_found", error_message=str(exc))
        base_url = validate_model_studio_base_url(self._settings.resolved_model_studio_base_url)

        try:
            response = self._http_client.get(
                f"{base_url}/tasks/{provider_task_id}",
                headers={"Authorization": f"Bearer {self._settings.media_api_key}"},
                timeout=30.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            with self._artifact_store.job_transaction(job_id):
                record = self._load_job(job_id)
                record["attempts"] = int(record.get("attempts", 0)) + 1
                record["error_code"] = "network_video_poll_timeout"
                record["error_message"] = "Video poll timed out"
                record["next_poll_at"] = _isoformat(
                    now_dt
                    + timedelta(seconds=min(self._settings.provider_backoff_cap_seconds, self._settings.provider_backoff_base_seconds * (2 ** (record["attempts"] - 1))))
                )
                try:
                    record = self._artifact_store.save_video_job(job_id, record, expected_version=expected_version)
                except StaleJobVersionError:
                    record = self._load_job(job_id)
                    return self._success(data={"job": self._artifact_store.public_job_view(record)})
                return self._success(data={"job": self._artifact_store.public_job_view(record)}, error_code=record["error_code"], error_message=record["error_message"])
        except Exception as exc:
            with self._artifact_store.job_transaction(job_id):
                record = self._load_job(job_id)
                record["attempts"] = int(record.get("attempts", 0)) + 1
                record["error_code"] = "provider_video_poll_failed"
                record["error_message"] = str(exc)
                record["next_poll_at"] = _isoformat(
                    now_dt
                    + timedelta(seconds=min(self._settings.provider_backoff_cap_seconds, self._settings.provider_backoff_base_seconds * (2 ** (record["attempts"] - 1))))
                )
                try:
                    record = self._artifact_store.save_video_job(job_id, record, expected_version=expected_version)
                except StaleJobVersionError:
                    record = self._load_job(job_id)
                    return self._success(data={"job": self._artifact_store.public_job_view(record)})
                return self._success(data={"job": self._artifact_store.public_job_view(record)}, error_code=record["error_code"], error_message=record["error_message"])

        with self._artifact_store.job_transaction(job_id):
            record = self._load_job(job_id)
            if record.get("cancellation_requested"):
                return self._success(data={"job": self._artifact_store.public_job_view(record)})
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["status"] = str(payload.get("output", {}).get("task_status") or "UNKNOWN")
            record["request_id"] = str(payload.get("request_id") or record.get("request_id") or "")
            record["usage"] = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            record["error_code"] = None
            record["error_message"] = None
            if record["status"] in {"PENDING", "RUNNING"}:
                delay = min(
                    self._settings.provider_backoff_cap_seconds,
                    self._settings.provider_backoff_base_seconds * (2 ** (record["attempts"] - 1)),
                )
                record["next_poll_at"] = _isoformat(now_dt + timedelta(seconds=delay))
            elif record["status"] == "SUCCEEDED":
                record["provider_result_url"] = payload.get("output", {}).get("video_url")
                record["provider_result_url_expires_at"] = _isoformat(now_dt + timedelta(hours=24))
                record["completed_at"] = _isoformat(now_dt)
                record["next_poll_at"] = None
            else:
                record["completed_at"] = _isoformat(now_dt)
                record["next_poll_at"] = None
                record["error_code"] = "provider_video_terminal"
                record["error_message"] = f"Provider reported terminal status {record['status']}"
            try:
                record = self._artifact_store.save_video_job(job_id, record, expected_version=expected_version)
            except StaleJobVersionError:
                record = self._load_job(job_id)
            return self._success(data={"job": self._artifact_store.public_job_view(record)})

    def cancel_video_job(self, job_id: str) -> dict[str, Any]:
        """Stop local polling for a job without claiming provider-side cancellation."""

        try:
            with self._artifact_store.job_transaction(job_id):
                record = self._load_job(job_id)
                record["cancellation_requested"] = True
                record["provider_cancel_supported"] = False
                record["provider_canceled"] = False
                record["next_poll_at"] = None
                record = self._artifact_store.save_video_job(job_id, record, expected_version=int(record.get("version", 0)))
        except Exception as exc:
            return self._success(data={"job_id": job_id}, error_code="state_video_job_not_found", error_message=str(exc))
        return self._success(data={"job": self._artifact_store.public_job_view(record)})

    def collect_video(self, job_id: str) -> dict[str, Any]:
        """Download the completed provider video result once and attach it to the local job record."""

        try:
            with self._artifact_store.job_transaction(job_id):
                record = self._load_job(job_id)
                if record.get("collected_artifact_id"):
                    manifest = self._artifact_store.load_artifact_manifest(str(record["collected_artifact_id"]))
                    return self._success(data={"job": self._artifact_store.public_job_view(record), "artifact": manifest})
                if record.get("status") != "SUCCEEDED":
                    return self._success(data={"job": self._artifact_store.public_job_view(record)}, error_code="state_video_not_collectable", error_message="Video job is not in SUCCEEDED state")
                if not record.get("provider_result_url"):
                    return self._success(data={"job": self._artifact_store.public_job_view(record)}, error_code="state_video_missing_url", error_message="No provider result URL is available")
                if record.get("provider_result_url_expires_at") and _isoformat(datetime.fromtimestamp(self._clock(), UTC)) > str(record["provider_result_url_expires_at"]):
                    return self._success(data={"job": self._artifact_store.public_job_view(record)}, error_code="provider_video_url_expired", error_message="The provider result URL expired before collection")
                expected_version = int(record.get("version", 0))
                downloaded = self._artifact_store.save_downloaded_artifact(
                    kind="video",
                    model_id=VIDEO_MODEL_ID,
                    prompt="redacted",
                    negative_prompt=None,
                    request_id=str(record.get("request_id") or ""),
                    latency_ms=0,
                    client=self._http_client,
                    download_url=str(record["provider_result_url"]),
                    usage=record.get("usage", {}),
                    cost=None,
                    mime_prefix="video/",
                    duration=record.get("duration"),
                    media_format="mp4",
                    seed=record.get("seed"),
                    prompt_hash=str(record.get("prompt_hash") or ""),
                    negative_prompt_hash=str(record.get("negative_prompt_hash") or "") or None,
                )
                record["collected_artifact_id"] = downloaded.artifact_id
                record["provider_result_url"] = None
                record["provider_result_url_expires_at"] = None
                record = self._artifact_store.save_video_job(job_id, record, expected_version=expected_version)
                return self._success(data={"job": self._artifact_store.public_job_view(record), "artifact": downloaded.manifest})
        except StaleJobVersionError:
            with self._artifact_store.job_transaction(job_id):
                record = self._load_job(job_id)
                if record.get("collected_artifact_id"):
                    manifest = self._artifact_store.load_artifact_manifest(str(record["collected_artifact_id"]))
                    return self._success(data={"job": self._artifact_store.public_job_view(record), "artifact": manifest})
                return self._success(data={"job": self._artifact_store.public_job_view(record)}, error_code="conflict_video_job_version", error_message="Video job state changed during collection")
        except ValueError as exc:
            return self._success(data={"job_id": job_id}, error_code="state_video_job_not_found", error_message=str(exc))
        except Exception as exc:
            return self._success(data={"job_id": job_id}, error_code="download_video_failed", error_message=str(exc))

    def inspect_video(self, artifact_id: str) -> dict[str, Any]:
        """Return the public manifest for a previously collected video artifact."""

        try:
            manifest = self._artifact_store.load_artifact_manifest(artifact_id)
        except Exception as exc:
            return self._success(data={"artifact_id": artifact_id}, error_code="state_video_not_found", error_message=str(exc))
        return self._success(data={"artifact": manifest})

    def list_pending_jobs(self) -> list[dict[str, Any]]:
        """Return pending local jobs for bounded recovery workflows."""

        return [self._artifact_store.public_job_view(record) for record in self._artifact_store.list_pending_jobs()]

    def resume_pending_jobs(self, max_jobs: int) -> list[dict[str, Any]]:
        """Poll at most `max_jobs` currently pending jobs once each."""

        limit = max(0, int(max_jobs))
        results: list[dict[str, Any]] = []
        for record in self._artifact_store.list_pending_jobs()[:limit]:
            results.append(self.get_video_job(str(record["job_id"])))
        return results
