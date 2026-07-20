"""Atomic persistence and hashing helpers for Layer B attempts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .models import AttemptRecord, canonical_json, relative_to


def hash_bytes(raw: bytes) -> str:
    """Return the SHA-256 digest for *raw*."""

    return hashlib.sha256(raw).hexdigest()


def hash_file(path: Path) -> str:
    """Return the SHA-256 digest for one file."""

    return hash_bytes(path.read_bytes())


def hash_tree(root: Path) -> str:
    """Return a deterministic tree hash for a directory."""

    entries: list[tuple[str, str]] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        entries.append((path.relative_to(root).as_posix(), hash_file(path)))
    return hash_bytes(json.dumps(entries, sort_keys=True).encode("utf-8"))


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 text to *path*."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically write canonical JSON to *path*."""

    atomic_write_text(path, canonical_json(payload))


def copy_tree(source: Path, destination: Path) -> None:
    """Copy one public fixture tree into a fresh destination."""

    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    shutil.copytree(source, destination)


class AttemptBundleWriter:
    """Helper for writing one attempt bundle without silent overwrite."""

    MANIFEST_NAME = "_bundle_manifest.json"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _refresh_manifest(self) -> None:
        """Persist the current bundle file inventory and hashes."""

        manifest: dict[str, dict[str, Any]] = {}
        for path in sorted(candidate for candidate in self.root.rglob("*") if candidate.is_file()):
            relative = relative_to(path, self.root)
            if relative == self.MANIFEST_NAME:
                continue
            raw = path.read_bytes()
            manifest[relative] = {
                "sha256": hash_bytes(raw),
                "size_bytes": len(raw),
                "content_kind": "json" if path.suffix == ".json" else "text" if _is_text_payload(raw) else "binary",
            }
        atomic_write_json(self.root / self.MANIFEST_NAME, manifest)

    def write_record(self, record: AttemptRecord) -> Path:
        """Persist the primary attempt record."""

        path = self.root / "attempt.json"
        atomic_write_json(path, record)
        self._refresh_manifest()
        return path

    def write_prompt(self, prompt_text: str) -> Path:
        """Persist the exact runner prompt."""

        path = self.root / "prompt.txt"
        atomic_write_text(path, prompt_text)
        self._refresh_manifest()
        return path

    def write_json(self, relative_path: str, payload: Any) -> Path:
        """Persist one arbitrary JSON payload inside the bundle."""

        path = self.root / relative_path
        atomic_write_json(path, payload)
        self._refresh_manifest()
        return path

    def write_text(self, relative_path: str, payload: str) -> Path:
        """Persist one arbitrary text payload inside the bundle."""

        path = self.root / relative_path
        atomic_write_text(path, payload)
        self._refresh_manifest()
        return path

    def reconstruct(self) -> dict[str, Any]:
        """Load the bundle back into a simple JSON-friendly mapping."""

        manifest_path = self.root / self.MANIFEST_NAME
        manifest: dict[str, dict[str, Any]] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result: dict[str, Any] = {}
        for path in sorted(candidate for candidate in self.root.rglob("*") if candidate.is_file()):
            relative = relative_to(path, self.root)
            if relative == self.MANIFEST_NAME:
                continue
            raw = path.read_bytes()
            expected = manifest.get(relative)
            if expected is not None:
                actual_hash = hash_bytes(raw)
                if actual_hash != expected.get("sha256"):
                    raise ValueError(f"bundle tamper detected for {relative}")
                if len(raw) != int(expected.get("size_bytes", len(raw))):
                    raise ValueError(f"bundle size mismatch detected for {relative}")
            if path.suffix == ".json":
                result[relative] = json.loads(raw.decode("utf-8"))
            else:
                result[relative] = raw.decode("utf-8") if _is_text_payload(raw) else raw
        if manifest:
            missing = sorted(set(manifest) - set(result))
            unexpected = sorted(set(result) - set(manifest))
            if missing:
                raise ValueError(f"bundle manifest is missing files: {missing}")
            if unexpected:
                raise ValueError(f"bundle manifest does not cover files: {unexpected}")
        return result


def _is_text_payload(raw: bytes) -> bool:
    """Return whether *raw* is safely decodable UTF-8 text."""

    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def classify_attempt_bundle(root: Path) -> dict[str, Any]:
    """Classify whether one persisted bundle is scorable, terminal-unscored, or orphaned."""

    attempt_path = root / "attempt.json"
    if not attempt_path.exists():
        return {"bundle_state": "orphaned", "scored": False, "reason": "attempt.json is missing"}
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    status = attempt.get("status")
    terminal_requirements = {
        "success": ["evaluator.json"],
        "failed": ["failure.json"],
        "refused": ["refusal.json"],
    }
    required = terminal_requirements.get(status, [])
    missing = [relative for relative in required if not (root / relative).exists()]
    if missing:
        return {
            "bundle_state": "orphaned",
            "scored": False,
            "reason": f"incomplete terminal evidence: {missing}",
            "attempt_status": status,
        }
    return {
        "bundle_state": "scored" if status == "success" else "terminal_unscored",
        "scored": status == "success",
        "attempt_status": status,
    }
