from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from config import normalize_media_base_url


DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
PRIVATE_FILE_MODE = 0o600
ARTIFACT_ID_PATTERN = re.compile(r"^artifact_[0-9a-f]{32}$")
JOB_ID_PATTERN = re.compile(r"^job_[0-9a-f]{32}$")
PUBLIC_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "kind",
        "local_relative_path",
        "mime_type",
        "byte_size",
        "sha256",
        "model_id",
        "prompt_hash",
        "negative_prompt_hash",
        "request_id",
        "latency_ms",
        "usage",
        "cost",
        "width",
        "height",
        "duration",
        "format",
        "seed",
        "source_artifact_ids",
        "source_audio_ids",
        "created_at",
        "published",
        "publish_purpose_hash",
        "publish_purpose_label",
        "provenance_complete",
    }
)
REDACTED_VALUE = "[redacted]"
_REDACTED_KEY_MARKERS = (
    "api_key",
    "authorization",
    "secret",
    "workspace_id",
    "provider_task_id",
    "provider_url",
    "result_url",
    "prompt",
    "negative_prompt",
    "url",
)
_URL_PATTERN = re.compile(r"https?://\S+")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _best_effort_private_permissions(path: Path) -> None:
    with suppress(OSError, NotImplementedError):
        os.chmod(path, PRIVATE_FILE_MODE)


def _fsync_parent_directory(path: Path) -> None:
    """Best-effort directory fsync after a replace to improve rename durability."""

    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
    except (AttributeError, FileNotFoundError, NotADirectoryError, OSError):
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            return
    finally:
        os.close(directory_fd)


def _is_forbidden_host(hostname: str) -> bool:
    lowered = hostname.lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_result_hostname(hostname: str) -> None:
    lowered = hostname.lower().rstrip(".")
    if _is_forbidden_host(lowered):
        raise ValueError("Provider result host is not allowed")
    if not lowered.endswith(".aliyuncs.com"):
        raise ValueError("Provider result host must be under aliyuncs.com")
    prefix = lowered[: -len(".aliyuncs.com")]
    labels = prefix.split(".")
    if any(not label for label in labels):
        raise ValueError("Provider result host is malformed")
    first_label = labels[0]
    joined = ".".join(labels)
    if not (first_label.startswith("dashscope-result") or "oss" in joined):
        raise ValueError("Provider result host is not an approved OSS/result host")


def validate_provider_media_url(url: str) -> str:
    """Validate a provider-owned result URL before any outbound media request."""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("Provider result URL must be a non-empty string")
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("Provider result URL must use https")
    if parsed.username or parsed.password:
        raise ValueError("Provider result URL must not include userinfo")
    if parsed.fragment:
        raise ValueError("Provider result URL must not include a fragment")
    if parsed.port not in (None, 443):
        raise ValueError("Provider result URL must use the default https port")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Provider result URL must include a hostname")
    _validate_result_hostname(hostname)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def validate_model_studio_base_url(base_url: str) -> str:
    """Validate and normalize the configured media service base URL.

    The legacy function name remains because callers still route image and video
    traffic through the same setting surface. Valid targets are limited to the
    Singapore DashScope intl host and Singapore workspace MAAS hosts.
    """

    normalized = normalize_media_base_url(base_url)
    if normalized:
        return normalized
    raise ValueError("Model Studio base URL must target the Singapore DashScope intl host or workspace media host")


class StaleJobVersionError(RuntimeError):
    """Raised when a video job save attempts to overwrite a newer local version."""


@dataclass(frozen=True)
class DownloadedArtifact:
    artifact_id: str
    manifest: dict[str, Any]


class MediaArtifactStore:
    """Durable local store for public media manifests and private video job records."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        directory_fsync: Callable[[Path], None] | None = None,
    ) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._clock = clock or _utc_now
        self._max_download_bytes = int(max_download_bytes)
        self._lock = threading.RLock()
        self._job_locks: dict[str, threading.RLock] = {}
        self._artifacts_dir = self._root_dir / "artifacts"
        self._manifests_dir = self._root_dir / "manifests"
        self._jobs_dir = self._root_dir / "private_jobs"
        self._directory_fsync = directory_fsync or _fsync_parent_directory
        for directory in (self._root_dir, self._artifacts_dir, self._manifests_dir, self._jobs_dir):
            directory.mkdir(parents=True, exist_ok=True)
            _best_effort_private_permissions(directory)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def hash_text(self, value: str | None) -> str | None:
        if not value:
            return None
        return _sha256_text(value)

    def new_artifact_id(self) -> str:
        return f"artifact_{secrets.token_hex(16)}"

    def new_job_id(self) -> str:
        return f"job_{secrets.token_hex(16)}"

    def _validate_artifact_id(self, artifact_id: str) -> str:
        if not isinstance(artifact_id, str) or not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise ValueError("artifact_id is invalid")
        return artifact_id

    def _validate_job_id(self, job_id: str) -> str:
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("job_id is invalid")
        return job_id

    def _contain_in(self, directory: Path, path: Path) -> Path:
        resolved = path.resolve()
        base = directory.resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError("Resolved path escapes its intended media store directory")
        return resolved

    def _manifest_path(self, artifact_id: str) -> Path:
        return self._contain_in(self._manifests_dir, self._manifests_dir / f"{self._validate_artifact_id(artifact_id)}.json")

    def _job_path(self, job_id: str) -> Path:
        return self._contain_in(self._jobs_dir, self._jobs_dir / f"{self._validate_job_id(job_id)}.json")

    def _artifact_path(self, artifact_id: str, suffix: str) -> Path:
        extension = suffix if suffix.startswith(".") else f".{suffix}"
        return self._contain_in(self._artifacts_dir, self._artifacts_dir / f"{self._validate_artifact_id(artifact_id)}{extension}")

    def _atomic_write_json(self, path: Path, payload: Mapping[str, Any], *, private: bool) -> None:
        parent = path.parent.resolve()
        target = self._contain_in(parent, path)
        temp_path = self._contain_in(parent, target.with_name(f"{target.name}.{secrets.token_hex(8)}.tmp"))
        data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            _best_effort_private_permissions(temp_path)
        os.replace(temp_path, target)
        self._directory_fsync(target)
        if private:
            _best_effort_private_permissions(target)

    def _read_json(self, path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _guess_suffix(self, mime_type: str, fallback_url: str) -> str:
        guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip()) or ""
        if guessed:
            return guessed
        candidate = Path(urlsplit(fallback_url).path).suffix.strip()
        if candidate and len(candidate) <= 8:
            return candidate
        return ".bin"

    def _job_lock(self, job_id: str) -> threading.RLock:
        validated_job_id = self._validate_job_id(job_id)
        with self._lock:
            lock = self._job_locks.get(validated_job_id)
            if lock is None:
                lock = threading.RLock()
                self._job_locks[validated_job_id] = lock
            return lock

    @contextmanager
    def job_transaction(self, job_id: str) -> Iterator[None]:
        """Serialize local updates for one video job within this backend process."""

        lock = self._job_lock(job_id)
        with lock:
            yield

    def _stream_download(
        self,
        *,
        client: httpx.Client,
        url: str,
        expected_prefix: str,
        destination: Path,
    ) -> tuple[str, int, str]:
        validated_url = validate_provider_media_url(url)
        temp_path = self._contain_in(destination.parent, destination.with_suffix(destination.suffix + ".part"))
        total_bytes = 0
        hasher = hashlib.sha256()
        try:
            with client.stream("GET", validated_url, timeout=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("Provider result redirect is not allowed")
                response.raise_for_status()
                validate_provider_media_url(str(response.url))
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not content_type.startswith(expected_prefix):
                    raise ValueError("Provider returned an unsupported media type")
                with open(temp_path, "wb") as handle:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > self._max_download_bytes:
                            raise ValueError("Downloaded media exceeded the configured size limit")
                        handle.write(chunk)
                        hasher.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temp_path, destination)
            self._directory_fsync(destination)
            _best_effort_private_permissions(destination)
            return content_type, total_bytes, hasher.hexdigest()
        except Exception:
            with suppress(FileNotFoundError):
                temp_path.unlink()
            with suppress(FileNotFoundError):
                destination.unlink()
            raise

    def save_downloaded_artifact(
        self,
        *,
        kind: str,
        model_id: str,
        prompt: str,
        negative_prompt: str | None,
        request_id: str,
        latency_ms: int,
        client: httpx.Client,
        download_url: str,
        usage: Mapping[str, Any] | None,
        cost: Mapping[str, Any] | None,
        mime_prefix: str,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        media_format: str | None = None,
        seed: int | None = None,
        source_artifact_ids: list[str] | None = None,
        source_audio_ids: list[str] | None = None,
        artifact_id: str | None = None,
        prompt_hash: str | None = None,
        negative_prompt_hash: str | None = None,
    ) -> DownloadedArtifact:
        with self._lock:
            local_artifact_id = self._validate_artifact_id(artifact_id or self.new_artifact_id())
            provisional_suffix = self._guess_suffix(mime_prefix, download_url)
            destination = self._artifact_path(local_artifact_id, provisional_suffix)
            content_type, byte_size, sha256 = self._stream_download(
                client=client,
                url=download_url,
                expected_prefix=mime_prefix,
                destination=destination,
            )
            final_suffix = self._guess_suffix(content_type, download_url)
            final_destination = self._artifact_path(local_artifact_id, final_suffix)
            if final_destination != destination:
                os.replace(destination, final_destination)
                self._directory_fsync(final_destination)
                destination = final_destination
            manifest = {
                "artifact_id": local_artifact_id,
                "kind": kind,
                "local_relative_path": destination.relative_to(self._root_dir).as_posix(),
                "mime_type": content_type,
                "byte_size": byte_size,
                "sha256": sha256,
                "model_id": model_id,
                "prompt_hash": prompt_hash or self.hash_text(prompt),
                "negative_prompt_hash": negative_prompt_hash or self.hash_text(negative_prompt),
                "request_id": request_id,
                "latency_ms": int(latency_ms),
                "usage": dict(usage or {}),
                "cost": dict(cost or {}),
                "width": _safe_int(width),
                "height": _safe_int(height),
                "duration": _safe_int(duration),
                "format": media_format or final_suffix.lstrip("."),
                "seed": _safe_int(seed),
                "source_artifact_ids": list(source_artifact_ids or []),
                "source_audio_ids": list(source_audio_ids or []),
                "created_at": _isoformat(self._clock()),
                "published": False,
                "publish_purpose_hash": None,
                "publish_purpose_label": None,
                "provenance_complete": True,
            }
            try:
                self._atomic_write_json(self._manifest_path(local_artifact_id), manifest, private=False)
            except Exception:
                with suppress(FileNotFoundError):
                    destination.unlink()
                raise
            return DownloadedArtifact(artifact_id=local_artifact_id, manifest=manifest)

    def load_artifact_manifest(self, artifact_id: str) -> dict[str, Any]:
        manifest = self._read_json(self._manifest_path(artifact_id))
        return {key: manifest.get(key) for key in PUBLIC_MANIFEST_FIELDS if key in manifest}

    def mark_artifact_published(self, artifact_id: str, purpose: str) -> dict[str, Any]:
        self._validate_artifact_id(artifact_id)
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("Purpose must be a non-empty string")
        trimmed = purpose.strip()
        if len(trimmed) > 200:
            raise ValueError("Purpose exceeds maximum length")
        sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_", " "} else "_" for ch in trimmed).strip()
        if not sanitized:
            raise ValueError("Purpose did not contain publishable characters")
        with self._lock:
            manifest = self._read_json(self._manifest_path(artifact_id))
            manifest["published"] = True
            manifest["publish_purpose_hash"] = _sha256_text(trimmed)
            manifest["publish_purpose_label"] = sanitized[:80]
            self._atomic_write_json(self._manifest_path(artifact_id), manifest, private=False)
            return {key: manifest.get(key) for key in PUBLIC_MANIFEST_FIELDS if key in manifest}

    def create_video_job(
        self,
        *,
        job_id: str | None,
        model_id: str,
        prompt: str,
        negative_prompt: str | None,
        request_id: str,
        provider_task_id: str,
        duration: int,
        size: str,
        resolution: str,
        ratio: str,
        seed: int | None,
        initial_status: str,
    ) -> dict[str, Any]:
        local_job_id = self._validate_job_id(job_id or self.new_job_id())
        now = _isoformat(self._clock())
        record = {
            "job_id": local_job_id,
            "version": 1,
            "model_id": model_id,
            "prompt_hash": self.hash_text(prompt),
            "negative_prompt_hash": self.hash_text(negative_prompt),
            "request_id": request_id,
            "provider_task_id": provider_task_id,
            "provider_result_url": None,
            "provider_result_url_expires_at": None,
            "status": initial_status,
            "duration": duration,
            "size": size,
            "resolution": resolution,
            "ratio": ratio,
            "seed": seed,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "submitted_at": now,
            "completed_at": None,
            "next_poll_at": now,
            "error_code": None,
            "error_message": None,
            "usage": {},
            "cancellation_requested": False,
            "provider_cancel_supported": False,
            "provider_canceled": False,
            "collected_artifact_id": None,
            "task_expires_at": _isoformat(self._clock().replace(microsecond=0) + timedelta(hours=24)),
        }
        with self.job_transaction(local_job_id):
            self._atomic_write_json(self._job_path(local_job_id), record, private=True)
        return record

    def load_video_job(self, job_id: str) -> dict[str, Any]:
        record = self._read_json(self._job_path(job_id))
        if "version" not in record:
            record["version"] = 0
        return record

    def save_video_job(self, job_id: str, record: Mapping[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        validated_job_id = self._validate_job_id(job_id)
        current = self.load_video_job(validated_job_id)
        current_version = int(current.get("version", 0))
        if expected_version is not None and current_version != int(expected_version):
            raise StaleJobVersionError("video job version conflict")
        payload = dict(record)
        payload["job_id"] = validated_job_id
        payload["version"] = current_version + 1
        payload["updated_at"] = _isoformat(self._clock())
        self._atomic_write_json(self._job_path(validated_job_id), payload, private=True)
        return payload

    def list_pending_jobs(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for job_file in sorted(self._jobs_dir.glob("*.json")):
            record = self._read_json(self._contain_in(self._jobs_dir, job_file))
            if "version" not in record:
                record["version"] = 0
            if record.get("status") in {"PENDING", "RUNNING"} and not record.get("cancellation_requested"):
                pending.append(record)
        return pending

    def redact_value(self, value: Any, *, extra_secrets: list[str] | None = None, key_hint: str | None = None) -> Any:
        if key_hint and any(marker in key_hint.lower() for marker in _REDACTED_KEY_MARKERS):
            return REDACTED_VALUE
        if isinstance(value, Mapping):
            return {
                key: self.redact_value(item, extra_secrets=extra_secrets, key_hint=str(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item, extra_secrets=extra_secrets, key_hint=key_hint) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item, extra_secrets=extra_secrets, key_hint=key_hint) for item in value)
        if isinstance(value, str):
            redacted = value
            for secret in extra_secrets or []:
                if secret:
                    redacted = redacted.replace(secret, REDACTED_VALUE)
            return _URL_PATTERN.sub(REDACTED_VALUE, redacted)
        return value

    def public_job_view(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "job_id": record.get("job_id"),
            "status": record.get("status"),
            "attempts": record.get("attempts"),
            "version": int(record.get("version", 0) or 0),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "submitted_at": record.get("submitted_at"),
            "completed_at": record.get("completed_at"),
            "next_poll_at": record.get("next_poll_at"),
            "duration": record.get("duration"),
            "size": record.get("size"),
            "resolution": record.get("resolution"),
            "ratio": record.get("ratio"),
            "seed": record.get("seed"),
            "usage": record.get("usage", {}),
            "error_code": record.get("error_code"),
            "error_message": record.get("error_message"),
            "error_category": self._error_category(record.get("error_code")),
            "cancellation_requested": bool(record.get("cancellation_requested")),
            "provider_cancel_supported": bool(record.get("provider_cancel_supported")),
            "provider_canceled": bool(record.get("provider_canceled")),
            "collected_artifact_id": record.get("collected_artifact_id"),
        }
        secrets_to_redact = [
            str(record.get("provider_task_id") or ""),
            str(record.get("provider_result_url") or ""),
        ]
        return self.redact_value(payload, extra_secrets=secrets_to_redact)

    @staticmethod
    def _error_category(error_code: Any) -> str | None:
        if not error_code:
            return None
        text = str(error_code)
        if text.startswith("config_"):
            return "configuration"
        if text.startswith("validation_"):
            return "validation"
        if text.startswith("state_"):
            return "state"
        if text.startswith("network_"):
            return "network"
        if text.startswith("provider_"):
            return "provider"
        if text.startswith("download_"):
            return "media"
        if text.startswith("conflict_"):
            return "conflict"
        return "tool"
