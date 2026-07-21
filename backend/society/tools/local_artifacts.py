from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from agno.tools import Toolkit

from ..capability_registry import get_role_capabilities

MAX_INSPECT_BYTES = 16_384
MAX_HASH_BYTES = 32 * 1024 * 1024
MAX_TEXT_CONTENT_CHARS = 4_000
MAX_REPORT_ITEMS = 12
MAX_REPORT_TEXT = 300
_URL_PATTERN = re.compile(r"https?://\S+")
_SAFE_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _safe_mime_type(path: Path, sample: bytes) -> str:
    """Return a conservative MIME type that is safe to expose to an agent."""

    guessed = mimetypes.guess_type(path.name)[0]
    if guessed in _SAFE_IMAGE_MIME_TYPES:
        if guessed != "image/png" or sample.startswith(b"\x89PNG\r\n\x1a\n"):
            return guessed
    return "application/octet-stream"


def _is_safe_utf8_text(value: str) -> bool:
    """Reject binary-looking UTF-8 so raw bytes never become model content."""

    return "\x00" not in value and all(char.isprintable() or char in "\n\r\t" for char in value)


def _bounded_text(value: Any, limit: int = MAX_REPORT_TEXT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else f"{text[:limit]}...[truncated]"


def _redact_text(text: str) -> str:
    redacted = _URL_PATTERN.sub("[redacted-url]", text)
    redacted = re.sub(r"\b(session|provider_task|task_id|job_id|handle|api_key|token|secret)[=:]\S+", r"\1=[redacted]", redacted, flags=re.IGNORECASE)
    return redacted


def _normalize_report_items(value: list[str] | dict[str, str] | str) -> list[str]:
    """Normalize common JSON-native validator shapes into bounded evidence lines."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            raw_items = [value]
        else:
            if isinstance(decoded, dict):
                raw_items = [f"{key}: {item}" for key, item in decoded.items()]
            elif isinstance(decoded, list):
                raw_items = decoded
            else:
                raw_items = [value]
    elif isinstance(value, Mapping):
        raw_items = [f"{key}: {item}" for key, item in value.items()]
    else:
        raw_items = list(value)
    return [_redact_text(_bounded_text(item)) for item in raw_items[:MAX_REPORT_ITEMS]]


def _raw_artifact_refs(value: list[str] | dict[str, str] | str) -> list[str]:
    """Extract reference values without turning mapping labels into fake paths."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            raw_items: list[Any] = [value]
        else:
            if isinstance(decoded, Mapping):
                raw_items = list(decoded.values())
            elif isinstance(decoded, list):
                raw_items = decoded
            else:
                raw_items = [value]
    elif isinstance(value, Mapping):
        raw_items = list(value.values())
    else:
        raw_items = list(value)
    return [str(item).strip() for item in raw_items[:MAX_REPORT_ITEMS] if str(item).strip()]


class LocalArtifactTools(Toolkit):
    """Read bounded task-local artifacts and emit side-effect-free validation reports."""

    def __init__(
        self,
        *,
        role_key: str,
        artifact_root: str | Path,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(name="local_artifact_tools", auto_register=False)
        self._role_key = role_key
        self._artifact_root = Path(artifact_root).resolve()
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._event_sink = event_sink
        self._authorized_tool_ids = {"inspect_artifact", "report_independent_validation"}
        self._require_authorization()
        for name in ("inspect_artifact", "report_independent_validation"):
            self.register(getattr(self, name))

    def _require_authorization(self) -> None:
        role = get_role_capabilities(self._role_key)
        if role is None:
            raise PermissionError(f"Unknown role {self._role_key!r}")
        allowed = set(getattr(role, "allowed_tools", []))
        if not self._authorized_tool_ids.issubset(allowed):
            raise PermissionError(f"Role {self._role_key!r} is not authorized for local artifact validation tools")

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._event_sink is None:
            return
        event = {"event_type": event_type, "role_key": self._role_key}
        for key, value in payload.items():
            event[key] = self._redact_value(value)
        self._event_sink(event)

    def _redact_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return _redact_text(value)
        if isinstance(value, Mapping):
            return {str(key)[:80]: self._redact_value(item) for key, item in list(value.items())[:MAX_REPORT_ITEMS]}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(item) for item in list(value)[:MAX_REPORT_ITEMS]]
        return _redact_text(_bounded_text(value))

    def _resolve_candidate(self, artifact_ref: str) -> tuple[Path, str]:
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise ValueError("artifact_ref must be a non-empty string")
        raw = artifact_ref.strip()
        candidate = Path(raw)
        joined = candidate if candidate.is_absolute() else (self._artifact_root / candidate)
        try:
            lexical_relative = joined.relative_to(self._artifact_root)
        except ValueError as exc:
            raise ValueError("Artifact path is outside the task artifact root") from exc
        current = self._artifact_root
        for part in lexical_relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Symlink artifact paths are not allowed")
        resolved = joined.resolve(strict=True)
        try:
            relative = resolved.relative_to(self._artifact_root)
        except ValueError as exc:
            raise ValueError("Artifact path resolves outside the task artifact root") from exc
        if resolved.is_dir():
            raise ValueError("Artifact inspection only accepts files")
        safe_relative = relative.as_posix()
        return resolved, safe_relative

    def inspect_artifact(self, artifact_ref: str) -> dict[str, Any]:
        """Return bounded metadata and safe content for one task-local artifact file."""

        try:
            resolved, safe_relative = self._resolve_candidate(artifact_ref)
            size_bytes = resolved.stat().st_size
            if size_bytes > MAX_HASH_BYTES:
                raise ValueError("Artifact exceeds the safe inspection limit")
            digest = hashlib.sha256()
            sample = bytearray()
            with resolved.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    digest.update(chunk)
                    if len(sample) < MAX_INSPECT_BYTES:
                        sample.extend(chunk[: MAX_INSPECT_BYTES - len(sample)])
            sha256 = digest.hexdigest()
            sample_bytes = bytes(sample)
            mime_type = _safe_mime_type(resolved, sample_bytes)
            text_content: str | None = None
            decoded = None
            if size_bytes <= MAX_INSPECT_BYTES:
                try:
                    decoded = sample_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = None
            if decoded is not None and _is_safe_utf8_text(decoded):
                text_content = _redact_text(decoded[:MAX_TEXT_CONTENT_CHARS])
                mime_type = "text/plain"
            width: int | None = None
            height: int | None = None
            if mime_type == "image/png" and len(sample_bytes) >= 24:
                width = int.from_bytes(sample_bytes[16:20], "big")
                height = int.from_bytes(sample_bytes[20:24], "big")
            payload = {
                "success": True,
                "artifact_ref": safe_relative,
                "safe_relative_path": safe_relative,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "mime_type": mime_type,
                "is_text": text_content is not None,
                "content": text_content,
                "width": width,
                "height": height,
            }
            self._emit(
                "local_artifact_inspected",
                artifact_ref=safe_relative,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
                width=width,
                height=height,
            )
            return payload
        except Exception as exc:
            self._emit(
                "local_artifact_inspection_failed",
                artifact_ref="invalid-artifact-ref",
                error_message="Artifact inspection failed",
            )
            return {
                "success": False,
                "artifact_ref": "invalid-artifact-ref",
                "error_code": "artifact_inspection_failed",
                "error_message": "Artifact inspection failed",
            }

    def report_independent_validation(
        self,
        passed: bool,
        checks: list[str] | dict[str, str] | str,
        failures: list[str] | dict[str, str] | str | None = None,
        inspected_artifact_refs: list[str] | dict[str, str] | str | None = None,
        summary: str = "",
        failed_checks: list[str] | dict[str, str] | str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded structured validation report with no external side effects.

        ``failed_checks`` is accepted as a provider-friendly alias and is
        normalized into the canonical ``failures`` output.
        """

        normalized_checks = _normalize_report_items(checks)
        normalized_failures = _normalize_report_items(failures or [])
        normalized_failures.extend(
            item for item in _normalize_report_items(failed_checks or [])
            if item not in normalized_failures
        )
        normalized_failures = normalized_failures[:MAX_REPORT_ITEMS]
        normalized_refs: list[str] = []
        for raw_ref in _raw_artifact_refs(inspected_artifact_refs or []):
            candidate = Path(raw_ref)
            if candidate.is_absolute():
                try:
                    safe_ref = candidate.resolve().relative_to(self._artifact_root).as_posix()
                except (OSError, ValueError):
                    continue
            else:
                portable = PurePosixPath(raw_ref.replace("\\", "/"))
                if portable.is_absolute() or not portable.parts or any(
                    part in {"", ".", ".."} or ":" in part for part in portable.parts
                ):
                    continue
                safe_ref = portable.as_posix()
            if safe_ref not in normalized_refs:
                normalized_refs.append(safe_ref)
        report = {
            "passed": bool(passed) and not normalized_failures,
            "checks": normalized_checks,
            "failures": normalized_failures,
            "inspected_artifact_refs": normalized_refs,
            "summary": _redact_text(_bounded_text(summary)),
        }
        self._emit(
            "local_independent_validation_reported",
            passed=report["passed"],
            checks=normalized_checks,
            failures=normalized_failures,
            failure_count=len(normalized_failures),
            inspected_artifact_refs=normalized_refs,
        )
        return report
