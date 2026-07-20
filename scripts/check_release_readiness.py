"""Fail closed when the Qwendom public submission package is incomplete.

This checker intentionally covers repository/package gates rather than runtime
correctness. Run backend tests, the frontend build, and browser QA separately.
Deleted-but-not-yet-committed files are ignored because they are absent from the
candidate working-tree publish set.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/ALIBABA_CLOUD_DEPLOYMENT.md",
    "docs/BENCHMARK.md",
    "docs/HACKATHON_ARCHITECTURE.md",
    "docs/SOCIETY_RUNTIME.md",
    "docs/THIRD_PARTY_NOTICES.md",
)
FORBIDDEN_TRACKED = re.compile(
    r"(\.log$|\.jsonl$|\.sqlite3?$|(^|/)node_modules/|(^|/)dist/|\.env\.local$)",
    re.IGNORECASE,
)
ALLOWED_TRACKED_RUNTIME_FILES = {
    "backend/tests/fixtures/fixed_specialist_ui_events.jsonl",
}


def candidate_tracked_files() -> list[str]:
    """Return Git-indexed paths that still exist in the candidate worktree."""

    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        path.strip().replace("\\", "/")
        for path in completed.stdout.splitlines()
        if path.strip() and (ROOT / path.strip()).exists()
    ]


def main() -> int:
    failures: list[str] = []

    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            failures.append(f"missing required file: {relative}")

    forbidden = [
        path
        for path in candidate_tracked_files()
        if path not in ALLOWED_TRACKED_RUNTIME_FILES and FORBIDDEN_TRACKED.search(path)
    ]
    failures.extend(f"forbidden tracked runtime file: {path}" for path in forbidden)

    benchmark = (ROOT / "docs/BENCHMARK.md").read_text(encoding="utf-8")
    status_match = re.search(r"BENCHMARK_STATUS:\s*([a-z_]+)", benchmark)
    benchmark_status = status_match.group(1) if status_match else "missing"
    if benchmark_status != "final":
        failures.append(f"benchmark status is {benchmark_status}, expected final")

    if failures:
        print("RELEASE NOT READY")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("RELEASE PACKAGE READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
