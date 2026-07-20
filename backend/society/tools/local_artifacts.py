from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from agno.tools import Toolkit

from ..capability_registry import get_role_capabilities

MAX_INSPECT_BYTES = 16_384
MAX_TEXT_CONTENT_CHARS = 4_000
MAX_REPORT_ITEMS = 12
MAX_REPORT_TEXT = 300
_URL_PATTERN = re.compile(r"https?://\S+")


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
        resolved = joined.resolve(strict=True)
        try:
            relative = resolved.relative_to(self._artifact_root)
        except ValueError as exc:
            raise ValueError("Artifact path resolves outside the task artifact root") from exc
        current = self._artifact_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Symlink artifact paths are not allowed")
        if resolved.is_dir():
            raise ValueError("Artifact inspection only accepts files")
        size_bytes = resolved.stat().st_size
        if size_bytes > MAX_INSPECT_BYTES:
            raise ValueError("Artifact exceeds the inspection size limit")
        safe_relative = relative.as_posix()
        return resolved, safe_relative

    def inspect_artifact(self, artifact_ref: str) -> dict[str, Any]:
        """Return bounded metadata and safe content for one task-local artifact file."""

        try:
            resolved, safe_relative = self._resolve_candidate(artifact_ref)
            raw_bytes = resolved.read_bytes()
            sha256 = hashlib.sha256(raw_bytes).hexdigest()
            mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            text_content: str | None = None
            try:
                decoded = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                decoded = None
            if decoded is not None:
                text_content = _redact_text(decoded[:MAX_TEXT_CONTENT_CHARS])
                mime_type = "text/plain"
            payload = {
                "success": True,
                "artifact_ref": safe_relative,
                "safe_relative_path": safe_relative,
                "sha256": sha256,
                "size_bytes": len(raw_bytes),
                "mime_type": mime_type,
                "is_text": text_content is not None,
                "content": text_content,
            }
            self._emit("local_artifact_inspected", artifact_ref=safe_relative, sha256=sha256, size_bytes=len(raw_bytes))
            return payload
        except Exception as exc:
            self._emit(
                "local_artifact_inspection_failed",
                artifact_ref=_redact_text(_bounded_text(artifact_ref)),
                error_message=_redact_text(_bounded_text(exc)),
            )
            return {
                "success": False,
                "artifact_ref": _redact_text(_bounded_text(artifact_ref)),
                "error_code": "artifact_inspection_failed",
                "error_message": _redact_text(_bounded_text(exc)),
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
        normalized_refs = _normalize_report_items(inspected_artifact_refs or [])
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
