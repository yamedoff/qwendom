from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.reporting_v2 import aggregate_mode
from benchmarks.runtime import TrialUsage
from benchmarks.runtime_v2 import SuiteTrialResult
from benchmarks.runners_v2 import run_single_task, run_society_task
from benchmarks.fixtures.answers_v2 import ANSWER_KEYS
from benchmarks.suite_v2 import BenchmarkAnswer
from config import Settings
from society.orchestrator import SocietyOrchestrator


def perfect(task_id: str) -> BenchmarkAnswer:
    key = ANSWER_KEYS[task_id]
    return BenchmarkAnswer(
        selected_ids=sorted(key.selected_ids), ordered_ids=key.ordered_ids,
        evidence_citations={k: sorted(v) for k, v in key.evidence_citations.items()},
        constraint_ids=sorted(key.constraint_ids), numeric_answers=key.numeric_answers,
    )


class Agent:
    def __init__(self, answer): self.answer = answer
    async def arun(self, prompt): return SimpleNamespace(content=self.answer, metrics=None, tools=[])


class Events:
    def list(self, task_id): return []


class Orchestrator:
    def __init__(self, status="complete"):
        self.status = status
        self.events = Events(); self.tasks = {}; self.session_states = {}
    def submit(self, prompt):
        task = SimpleNamespace(id="runtime-1", status="running", final_answer=None)
        self.tasks[task.id] = task; self.session_states[task.id] = {}
        return task
    async def run_task(self, task_id): self.tasks[task_id].status = self.status
    def model_usage_summary(self, task_id):
        return {"total_calls": 2, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "usage_complete": True}
    def _capture_model_usage(self, *args): pass


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(LLM_PROVIDER="qwen", QWEN_API_KEY="x", QWEN_MODEL="qwen3.7-plus")

    def test_single_and_society_use_same_prompt_and_score(self):
        task_id = "constraint-aware-decision"; answer = perfect(task_id)
        single = asyncio.run(run_single_task(self.settings, task_id, agent_factory=lambda _: Agent(answer)))
        society = asyncio.run(run_society_task(
            self.settings, task_id, orchestrator_factory=lambda _: Orchestrator(),
            synthesis_factory=lambda _prompt, _bundle: Agent(answer),
        ))
        self.assertEqual(single.prompt_hash, society.prompt_hash)
        self.assertEqual(single.evaluation["score"], 100)
        self.assertEqual(society.evaluation["score"], 100)

    def test_report_macro_averages_tasks_not_attempt_counts(self):
        trials = []
        for task_id in ANSWER_KEYS:
            trials.append(asyncio.run(run_single_task(
                self.settings, task_id, agent_factory=lambda _prompt, tid=task_id: Agent(perfect(tid))
            )))
        self.assertEqual(aggregate_mode(trials)["macro_mean_score"], 100)

    def test_complete_with_warnings_is_scored_and_preserved(self):
        task_id = "evidence-verification"; answer = perfect(task_id)
        result = asyncio.run(run_society_task(
            self.settings, task_id,
            orchestrator_factory=lambda _: Orchestrator("complete_with_warnings"),
            synthesis_factory=lambda _prompt, _bundle: Agent(answer),
        ))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.society_terminal_status, "complete_with_warnings")
        self.assertEqual(result.evaluation["score"], 100)

    def test_failed_attempt_usage_is_not_hidden(self):
        failed = SuiteTrialResult(
            task_id="incident-diagnosis", task_kind="incident_diagnosis",
            mode="society", provider="qwen", model="qwen3.7-plus", prompt_hash="x",
            wall_duration_s=12, usage=TrialUsage(
                model_calls=7, input_tokens=90, output_tokens=10, total_tokens=100,
            ), error="failed before synthesis",
        )
        report = aggregate_mode([failed])
        self.assertEqual(report["model_calls"], 7)
        self.assertEqual(report["total_tokens"], 100)
        self.assertEqual(report["mean_duration_seconds"], 12)

    def test_orchestrator_honors_isolated_event_store_setting(self):
        path = Path(tempfile.mkdtemp()) / "benchmark-events.jsonl"
        settings = self.settings.model_copy(update={"event_store_file": str(path)})
        with patch("society.orchestrator.get_agno_db", return_value=None):
            orchestrator = SocietyOrchestrator(settings=settings)
        self.assertEqual(orchestrator.events.path, path)


if __name__ == "__main__": unittest.main()
