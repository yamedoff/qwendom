from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks import IncidentDecision
from benchmarks.fixtures.ground_truth import (
    GROUND_TRUTH_ACTION_ORDER, GROUND_TRUTH_CONTROLS,
    GROUND_TRUTH_CONSTRAINT_SATISFACTION, GROUND_TRUTH_HYPOTHESIS_EVIDENCE,
    GROUND_TRUTH_HYPOTHESIS_ID, GROUND_TRUTH_REQUIRED_CLAIMS,
)
from benchmarks.society import run_society
from config import Settings


def _decision() -> IncidentDecision:
    return IncidentDecision(
        root_cause_hypothesis_id=GROUND_TRUTH_HYPOTHESIS_ID,
        hypothesis_evidence=list(GROUND_TRUTH_HYPOTHESIS_EVIDENCE),
        recommended_actions=list(GROUND_TRUTH_ACTION_ORDER),
        recommended_controls=list(GROUND_TRUTH_CONTROLS),
        evidence_citations={k: list(v) for k, v in GROUND_TRUTH_REQUIRED_CLAIMS.items()},
        stakeholder_constraints_acknowledged=list(GROUND_TRUTH_CONSTRAINT_SATISFACTION),
    )


class _Events:
    def list(self, task_id):
        return [SimpleNamespace(type="proposal_submitted", actor="architect", payload={"proposal": "x"})]


class _Orchestrator:
    def __init__(self, settings, status="complete"):
        self.status = status
        self.events = _Events()
        self.tasks = {}
        self.session_states = {}
        self.captured = []

    def submit(self, prompt):
        task = SimpleNamespace(id="task-1", status="running", final_answer=None)
        self.tasks[task.id] = task
        self.session_states[task.id] = {"proposals": {"architect": {"proposal": "x"}}}
        return task

    async def run_task(self, task_id):
        self.tasks[task_id].status = self.status
        self.tasks[task_id].final_answer = "work"

    def _capture_model_usage(self, task_id, response, call_kind, actor_id):
        self.captured.append(call_kind)

    def model_usage_summary(self, task_id):
        return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": 0, "cost": 0.01, "duration": 1.0,
                "usage_complete": True}


class _Agent:
    async def arun(self, prompt):
        return SimpleNamespace(content=_decision(), metrics=None)


class SocietyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(LLM_PROVIDER="qwen", QWEN_API_KEY="x", QWEN_MODEL="qwen3.7-plus")

    def test_success(self):
        orch = _Orchestrator(self.settings)
        result = asyncio.run(run_society(
            self.settings,
            orchestrator_factory=lambda settings: orch,
            synthesis_agent_factory=lambda prompt, bundle: _Agent(),
            clock=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
        ))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.evaluation.total_score, 100)
        self.assertEqual(result.usage.total_tokens, 15)
        self.assertEqual(result.governance_trace[0]["type"], "proposal_submitted")
        self.assertIn("benchmark_synthesis", orch.captured)

    def test_non_complete_society_is_preserved_as_failure(self):
        orch = _Orchestrator(self.settings, status="waiting_for_user")
        result = asyncio.run(run_society(
            self.settings,
            orchestrator_factory=lambda settings: orch,
            synthesis_agent_factory=lambda prompt, bundle: _Agent(),
        ))
        self.assertEqual(result.status, "failed")
        self.assertIn("waiting_for_user", result.error)

    def test_prompt_hash_matches_single_agent(self):
        from benchmarks.loader import build_prompt
        from benchmarks.runtime import hash_text
        orch = _Orchestrator(self.settings)
        result = asyncio.run(run_society(
            self.settings,
            orchestrator_factory=lambda settings: orch,
            synthesis_agent_factory=lambda prompt, bundle: _Agent(),
        ))
        self.assertEqual(result.prompt_hash, hash_text(build_prompt("single_agent")))

    def test_token_budget_overrun_is_preserved_as_failure(self):
        orch = _Orchestrator(self.settings)
        result = asyncio.run(run_society(
            self.settings,
            orchestrator_factory=lambda settings: orch,
            synthesis_agent_factory=lambda prompt, bundle: _Agent(),
            max_total_tokens=14,
        ))
        self.assertEqual(result.status, "failed")
        self.assertIn("token budget exceeded", result.error)


if __name__ == "__main__":
    unittest.main()
