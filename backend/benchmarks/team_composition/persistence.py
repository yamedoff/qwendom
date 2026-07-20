"""Atomic persistence helpers for Layer A benchmark evidence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from benchmarks.team_composition.models import EvaluatedTrialRecord, PublicTrialRecord, canonical_json


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def next_trial_index(output_dir: Path, scenario_id: str) -> int:
    """Return the next deterministic trial index for a scenario."""

    folder = _ensure_directory(output_dir / scenario_id)
    highest = 0
    for candidate in folder.glob("*trial-*.json"):
        stem = candidate.stem
        suffix = stem.split("-")[-1]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def make_trial_id(scenario_id: str, index: int) -> str:
    """Return a deterministic trial ID within one scenario directory."""

    return f"{scenario_id}-trial-{index:03d}"


def _atomic_write(path: Path, content: str) -> None:
    _ensure_directory(path.parent)
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


def persist_public_trial(output_dir: Path, record: PublicTrialRecord) -> Path:
    """Persist a public pre-evaluation record without silent overwrite."""

    target = output_dir / record.scenario_id / f"{record.trial_id}.public.json"
    if target.exists():
        raise FileExistsError(f"public trial record already exists: {target}")
    _atomic_write(target, canonical_json(record))
    return target


def persist_evaluated_trial(output_dir: Path, record: EvaluatedTrialRecord) -> Path:
    """Persist a post-evaluation record without silent overwrite."""

    target = output_dir / record.scenario_id / f"{record.trial_id}.json"
    if target.exists():
        raise FileExistsError(f"evaluated trial record already exists: {target}")
    _atomic_write(target, canonical_json(record))
    return target


def load_evaluated_trials(output_dir: Path) -> list[EvaluatedTrialRecord]:
    """Load all persisted evaluated trials in deterministic path order."""

    records: list[EvaluatedTrialRecord] = []
    for path in sorted(output_dir.glob("*/*.json")):
        if path.name.endswith(".public.json"):
            continue
        records.append(EvaluatedTrialRecord.model_validate_json(path.read_text(encoding="utf-8")))
    return records
