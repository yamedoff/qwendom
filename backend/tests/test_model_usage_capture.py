from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeMetrics:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeResponse:
    def __init__(self, metrics: _FakeMetrics | None = None, model: str | None = None):
        self.metrics = metrics
        self.model = model
        self.content = "ok"


class _MinimalOrchestrator:
    """Construct just enough of SocietyOrchestrator to exercise capture/summary."""

    def __init__(self) -> None:
        from society.orchestrator import SocietyOrchestrator

        self._state_store: dict[str, dict] = {}
        self._capture = SocietyOrchestrator._capture_model_usage
        self._summary = SocietyOrchestrator.model_usage_summary

    def _state(self, task_id: str) -> dict:
        return self._state_store.setdefault(task_id, {})

    def capture(self, task_id: str, response: object, call_kind: str, actor_id: str) -> None:
        self._capture(self, task_id, response, call_kind, actor_id)

    def summary(self, task_id: str) -> dict:
        return self._summary(self, task_id)


class CaptureModelUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = _MinimalOrchestrator()

    def test_full_metrics_recorded(self) -> None:
        metrics = _FakeMetrics({
            "model": "qwen3.7-plus",
            "model_provider": "qwen",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "reasoning_tokens": 20,
            "cost": 0.003,
            "duration": 1.5,
            "time_to_first_token": 0.3,
        })
        response = _FakeResponse(metrics=metrics)
        self.orch.capture("task-1", response, "governance_tool", "architect")

        records = self.orch._state("task-1")["model_usage"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["metrics_present"])
        self.assertEqual(record["task_id"], "task-1")
        self.assertEqual(record["call_kind"], "governance_tool")
        self.assertEqual(record["actor_id"], "architect")
        self.assertEqual(record["model"], "qwen3.7-plus")
        self.assertEqual(record["model_provider"], "qwen")
        self.assertEqual(record["input_tokens"], 100)
        self.assertEqual(record["output_tokens"], 50)
        self.assertEqual(record["total_tokens"], 150)
        self.assertEqual(record["cache_read_tokens"], 10)
        self.assertEqual(record["cache_write_tokens"], 5)
        self.assertEqual(record["reasoning_tokens"], 20)
        self.assertAlmostEqual(record["cost"], 0.003)
        self.assertAlmostEqual(record["duration"], 1.5)
        self.assertAlmostEqual(record["time_to_first_token"], 0.3)

    def test_missing_metrics_marks_incomplete(self) -> None:
        response = _FakeResponse(metrics=None)
        self.orch.capture("task-2", response, "ask_agent", "builder")

        records = self.orch._state("task-2")["model_usage"]
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["metrics_present"])
        self.assertEqual(records[0]["input_tokens"], 0)
        self.assertIsNone(records[0]["cost"])

    def test_partial_cost_only(self) -> None:
        metrics = _FakeMetrics({"cost": 0.01})
        response = _FakeResponse(metrics=metrics)
        self.orch.capture("task-3", response, "governance_tool", "critic")

        records = self.orch._state("task-3")["model_usage"]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["metrics_present"])
        self.assertAlmostEqual(records[0]["cost"], 0.01)
        self.assertEqual(records[0]["input_tokens"], 0)
        self.assertEqual(records[0]["output_tokens"], 0)

    def test_missing_cost_remains_unknown(self) -> None:
        response = _FakeResponse(metrics=_FakeMetrics({"input_tokens": 10}))
        self.orch.capture("task-3a", response, "governance_tool", "critic")

        record = self.orch._state("task-3a")["model_usage"][0]
        self.assertIsNone(record["cost"])
        self.assertFalse(self.orch.summary("task-3a")["cost_complete"])

    def test_empty_metrics_dict_marks_incomplete(self) -> None:
        metrics = _FakeMetrics({})
        response = _FakeResponse(metrics=metrics)
        self.orch.capture("task-3b", response, "governance_tool", "critic")

        records = self.orch._state("task-3b")["model_usage"]
        self.assertFalse(records[0]["metrics_present"])

    def test_metrics_to_dict_raises_tolerated(self) -> None:
        class _BadMetrics:
            def to_dict(self):
                raise RuntimeError("provider error")

        response = _FakeResponse(metrics=_BadMetrics())
        self.orch.capture("task-3c", response, "governance_tool", "researcher")

        records = self.orch._state("task-3c")["model_usage"]
        self.assertFalse(records[0]["metrics_present"])

    def test_no_metrics_attribute_tolerated(self) -> None:
        response = _FakeResponse()
        delattr(response, "metrics")
        self.orch.capture("task-3d", response, "ask_agent", "builder")

        records = self.orch._state("task-3d")["model_usage"]
        self.assertFalse(records[0]["metrics_present"])

    def test_aggregation_across_repeated_calls(self) -> None:
        for i in range(3):
            metrics = _FakeMetrics({
                "input_tokens": 100 + i * 10,
                "output_tokens": 50 + i * 5,
                "total_tokens": 150 + i * 15,
                "cost": 0.001 * (i + 1),
                "duration": 0.5 + i * 0.1,
            })
            response = _FakeResponse(metrics=metrics)
            self.orch.capture("task-4", response, "governance_tool", "architect")

        summary = self.orch.summary("task-4")
        self.assertEqual(summary["total_calls"], 3)
        self.assertEqual(summary["input_tokens"], 100 + 110 + 120)
        self.assertEqual(summary["output_tokens"], 50 + 55 + 60)
        self.assertEqual(summary["total_tokens"], 150 + 165 + 180)
        self.assertAlmostEqual(summary["cost"], 0.001 + 0.002 + 0.003)
        self.assertTrue(summary["cost_complete"])
        self.assertAlmostEqual(summary["duration"], 0.5 + 0.6 + 0.7)
        self.assertTrue(summary["usage_complete"])

    def test_summary_usage_complete_false_when_metrics_missing(self) -> None:
        good_metrics = _FakeMetrics({"input_tokens": 50, "cost": 0.001})
        good = _FakeResponse(metrics=good_metrics)
        bad = _FakeResponse(metrics=None)
        self.orch.capture("task-5", good, "governance_tool", "architect")
        self.orch.capture("task-5", bad, "ask_agent", "builder")

        summary = self.orch.summary("task-5")
        self.assertEqual(summary["total_calls"], 2)
        self.assertFalse(summary["usage_complete"])

    def test_summary_no_calls_is_complete(self) -> None:
        summary = self.orch.summary("task-empty")
        self.assertEqual(summary["total_calls"], 0)
        self.assertTrue(summary["usage_complete"])

    def test_summary_aggregates_mixed_partial(self) -> None:
        full = _FakeResponse(metrics=_FakeMetrics({
            "input_tokens": 200,
            "output_tokens": 100,
            "total_tokens": 300,
            "cost": 0.005,
            "duration": 2.0,
            "reasoning_tokens": 30,
        }))
        partial = _FakeResponse(metrics=_FakeMetrics({"cost": 0.002}))
        self.orch.capture("task-6", full, "governance_tool", "architect")
        self.orch.capture("task-6", partial, "schema_json_retry", "builder")

        summary = self.orch.summary("task-6")
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["input_tokens"], 200)
        self.assertEqual(summary["output_tokens"], 100)
        self.assertAlmostEqual(summary["cost"], 0.007)
        self.assertEqual(summary["reasoning_tokens"], 30)
        self.assertTrue(summary["usage_complete"])

    def test_fallback_model_from_response_attribute(self) -> None:
        response = _FakeResponse(metrics=_FakeMetrics({"input_tokens": 10}))
        response.model = "qwen-max"
        self.orch.capture("task-7", response, "ask_agent", "researcher")

        records = self.orch._state("task-7")["model_usage"]
        self.assertEqual(records[0]["model"], "qwen-max")

    def test_does_not_alter_response(self) -> None:
        metrics = _FakeMetrics({"input_tokens": 42, "cost": 0.001})
        response = _FakeResponse(metrics=metrics, model="test-model")
        original_content = response.content
        self.orch.capture("task-8", response, "governance_tool", "architect")
        self.assertEqual(response.content, original_content)
        self.assertIs(response.metrics, metrics)


if __name__ == "__main__":
    unittest.main()
