"""Contract, runner, and reporting tests for benchmark suite v3."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.evaluator_v3 import evaluate
from benchmarks.fixtures.answers_v3 import (
    EXPECTED_CONSTRAINTS,
    EXPECTED_EVIDENCE,
    EXPECTED_FINDINGS,
    EXPECTED_NUMERICS,
    EXPECTED_STAGES,
)
from benchmarks.loader_v3 import build_prompt
from benchmarks.reporting_v3 import aggregate_mode, compare_modes
from benchmarks.runners_v3 import (
    MAX_TOOL_CALLS, REQUIRED_SURFACE_TOOLS, run_single_task, run_society_task,
)
from benchmarks.runtime import TrialUsage
from benchmarks.runtime_v2 import ToolCallRecord
from benchmarks.runtime_v3 import SuiteV3TrialResult
from benchmarks.suite_v2 import BenchmarkAnswer
from benchmarks.tools_v3 import TOOL_NAMES, inspect_auth_surface, lookup_security_record
from config import Settings


def perfect_answer() -> BenchmarkAnswer:
    """Build the exact private-key answer for deterministic evaluator tests."""

    return BenchmarkAnswer(
        selected_ids=sorted(EXPECTED_FINDINGS),
        ordered_ids=EXPECTED_STAGES,
        evidence_citations={key: sorted(value) for key, value in EXPECTED_EVIDENCE.items()},
        constraint_ids=sorted(EXPECTED_CONSTRAINTS),
        numeric_answers=EXPECTED_NUMERICS,
    )


class Agent:
    """Minimal asynchronous agent double used without paid provider calls."""

    def __init__(self, answer: BenchmarkAnswer, tools: list | None = None):
        self.answer = answer
        self.tools = tools or []

    async def arun(self, prompt: str):
        return SimpleNamespace(content=self.answer, metrics=None, tools=self.tools)


class Events:
    """In-memory event list double for society runner tests."""

    def __init__(self, events: list | None = None):
        self.events = events or []

    def list(self, task_id: str):
        return self.events


class Orchestrator:
    """Small society orchestrator double preserving runner-facing behavior."""

    def __init__(self, status: str = "complete", events: list | None = None):
        self.status = status
        self.events = Events(events)
        self.tasks: dict = {}
        self.session_states: dict = {}

    def submit(self, prompt: str):
        task = SimpleNamespace(id="runtime-v3", status="running", final_answer=None)
        self.tasks[task.id] = task
        self.session_states[task.id] = {}
        return task

    async def run_task(self, task_id: str):
        self.tasks[task_id].status = self.status

    def model_usage_summary(self, task_id: str):
        return {
            "total_calls": 2,
            "input_tokens": 90,
            "output_tokens": 10,
            "total_tokens": 100,
            "usage_complete": True,
        }

    def _capture_model_usage(self, *args):
        return None


class BenchmarkV3Tests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            LLM_PROVIDER="qwen", QWEN_API_KEY="x", QWEN_MODEL="qwen3.7-plus"
        )

    def test_prompt_exposes_contract_but_not_records_or_private_key(self):
        prompt = build_prompt()
        self.assertIn("inspect_auth_surface", prompt)
        self.assertIn("complete parallel critical-path timing", prompt)
        self.assertIn("do not count missing test coverage", prompt)
        self.assertNotIn("S01", prompt)
        self.assertNotIn("currently valid signing credential", prompt)
        self.assertNotIn("57.0", prompt)

    def test_evaluator_has_exactly_twenty_four_checks(self):
        result = evaluate(perfect_answer())
        self.assertEqual(len(result["checks"]), 24)
        self.assertEqual(result["acceptance_checks_total"], 24)
        self.assertEqual(result["acceptance_checks_passed"], 24)
        self.assertEqual(result["score"], 100)

    def test_extra_decoy_invalidates_every_finding_selection_check(self):
        answer = perfect_answer()
        answer.selected_ids.append("N01")
        result = evaluate(answer)
        self.assertFalse(any(
            passed for name, passed in result["checks"].items() if name.startswith("finding:")
        ))

    def test_wrong_stage_order_fails_affected_positions(self):
        answer = perfect_answer()
        answer.ordered_ids = ["G02", "G01", "G03", "G04"]
        result = evaluate(answer)
        self.assertFalse(result["checks"]["stage:G01"])
        self.assertFalse(result["checks"]["stage:G02"])
        self.assertTrue(result["checks"]["stage:G03"])

    def test_tools_return_public_records_and_self_correct_unknown_ids(self):
        self.assertEqual(inspect_auth_surface()[0]["record_id"], "S01")
        unknown = lookup_security_record("S99")
        self.assertFalse(unknown["found"])
        self.assertIn("S24", unknown["valid_record_ids"])
        self.assertEqual(len(TOOL_NAMES), 6)

    def test_single_and_society_share_prompt_score_and_v3_settings(self):
        answer = perfect_answer()
        seen_settings = []
        surface_calls = [
            SimpleNamespace(tool_name=name, tool_args={}) for name in REQUIRED_SURFACE_TOOLS
        ]
        single = asyncio.run(run_single_task(
            self.settings, agent_factory=lambda _: Agent(answer, surface_calls)
        ))
        society = asyncio.run(run_society_task(
            self.settings,
            orchestrator_factory=lambda cfg: seen_settings.append(cfg) or Orchestrator(),
            synthesis_factory=lambda _prompt, _bundle: Agent(answer, surface_calls),
        ))
        self.assertEqual(single.prompt_hash, society.prompt_hash)
        self.assertEqual(single.evaluation["score"], 100)
        self.assertEqual(society.evaluation["score"], 100)
        self.assertEqual(seen_settings[0].benchmark_suite_version, "v3")
        self.assertFalse(seen_settings[0].context7_mcp_enabled)

    def test_runner_prefetches_every_required_surface(self):
        result = asyncio.run(run_single_task(
            self.settings, agent_factory=lambda _: Agent(perfect_answer())
        ))
        self.assertEqual(result.status, "success")
        self.assertEqual({call.name for call in result.tool_calls}, REQUIRED_SURFACE_TOOLS)

    def test_shared_tool_budget_is_enforced_after_society_and_synthesis(self):
        events = [SimpleNamespace(
            type="benchmark_tool_used",
            payload={"name": "inspect_auth_surface", "arguments": {}},
        ) for _ in range(MAX_TOOL_CALLS)]
        synthesis_call = SimpleNamespace(tool_name="calculate", tool_args={"expression": "1+1"})
        result = asyncio.run(run_society_task(
            self.settings,
            orchestrator_factory=lambda _: Orchestrator(events=events),
            synthesis_factory=lambda _prompt, _bundle: Agent(perfect_answer(), [synthesis_call]),
        ))
        self.assertEqual(result.status, "failed")
        self.assertIn("tool-call budget exceeded", result.error)

    def test_reporting_calculates_quality_and_efficiency(self):
        trial = SuiteV3TrialResult(
            mode="single_agent", provider="qwen", model="qwen3.7-plus", prompt_hash="x",
            status="success", wall_duration_s=120,
            evaluation=evaluate(perfect_answer()),
            usage=TrialUsage(
                model_calls=1, input_tokens=900_000, output_tokens=100_000,
                total_tokens=1_000_000, usage_complete=True,
            ),
            tool_calls=[ToolCallRecord(name="inspect_auth_surface")],
        )
        aggregate = aggregate_mode([trial])
        self.assertEqual(aggregate["checks_per_minute"], 12)
        self.assertEqual(aggregate["checks_per_million_tokens"], 24)
        comparison = compare_modes([trial], [trial.model_copy(update={"mode": "society"})])
        self.assertEqual(comparison["quality_delta"], 0)
        self.assertEqual(comparison["checks_per_minute_delta"], 0)


if __name__ == "__main__":
    unittest.main()
