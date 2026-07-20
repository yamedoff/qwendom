"""Regression coverage for independent-validator terminal classification.

This exercises the no-provider validator path against an already-finalized
acceptance record. A validator failure must override a provisional successful
terminal state; it cannot be downgraded to ``complete_with_warnings``.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import TaskRun
from society.orchestrator import SocietyOrchestrator


def test_failed_independent_validator_makes_acceptance_terminally_failed() -> None:
    """A failed independent check is required, not an optional warning."""

    task = TaskRun(prompt="Create add.py, test it, and preserve provenance.")
    state = {
        "acceptance_evidence": {
            "terminal_status": "complete",
            "required_failures": [],
            "optional_failures": ["critique_exists"],
        },
        "acceptance_checks": [{"check": "required_artifacts", "passed": False}],
        "failed_checks": [{"check": "required_artifacts", "reason": "add.py is missing"}],
    }
    orchestrator = SocietyOrchestrator.__new__(SocietyOrchestrator)
    orchestrator.settings = SimpleNamespace(llm_enabled=False)
    orchestrator._state = lambda _task_id: state
    orchestrator._ensure_independent_validator = lambda _task, _team: SimpleNamespace(id="validator")
    orchestrator._register_artifact = lambda *args, **kwargs: None
    orchestrator._emit = lambda *args, **kwargs: None

    asyncio.run(orchestrator._run_independent_validator(task, SimpleNamespace()))

    evidence = state["acceptance_evidence"]
    assert evidence["terminal_status"] == "failed"
    assert "independent_validator" in evidence["required_failures"]
    assert "independent_validator" not in evidence["optional_failures"]
    assert evidence["terminal_status"] != "complete_with_warnings"
