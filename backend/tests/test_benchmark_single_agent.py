from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks import IncidentDecision, evaluate
from benchmarks.fixtures.ground_truth import (
    GROUND_TRUTH_ACTION_ORDER,
    GROUND_TRUTH_ACTIONS,
    GROUND_TRUTH_CONTROLS,
    GROUND_TRUTH_CONSTRAINT_SATISFACTION,
    GROUND_TRUTH_HYPOTHESIS_EVIDENCE,
    GROUND_TRUTH_HYPOTHESIS_ID,
    GROUND_TRUTH_REQUIRED_CLAIMS,
)
from benchmarks.runtime import (
    TrialResult,
    TrialUsage,
    extract_metrics,
    hash_text,
    parse_decision,
)
from benchmarks.single_agent import run_single_agent
from config import Settings


def _qwen_settings() -> Settings:
    return Settings(
        LLM_PROVIDER="qwen",
        QWEN_API_KEY="test-key",
        QWEN_MODEL="qwen3.7-plus",
    )


def _perfect_decision() -> IncidentDecision:
    return IncidentDecision(
        root_cause_hypothesis_id=GROUND_TRUTH_HYPOTHESIS_ID,
        hypothesis_evidence=list(GROUND_TRUTH_HYPOTHESIS_EVIDENCE),
        recommended_actions=list(GROUND_TRUTH_ACTION_ORDER),
        recommended_controls=list(GROUND_TRUTH_CONTROLS),
        evidence_citations={
            claim: list(fids)
            for claim, fids in GROUND_TRUTH_REQUIRED_CLAIMS.items()
        },
        stakeholder_constraints_acknowledged=list(
            GROUND_TRUTH_CONSTRAINT_SATISFACTION.keys()
        ),
        governance_activity=[],
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


class _FakeMetrics:
    def __init__(
        self,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        cost=None,
        duration=None,
        time_to_first_token=None,
    ):
        self._data = {}
        if input_tokens is not None:
            self._data["input_tokens"] = input_tokens
        if output_tokens is not None:
            self._data["output_tokens"] = output_tokens
        if total_tokens is not None:
            self._data["total_tokens"] = total_tokens
        if cache_read_tokens is not None:
            self._data["cache_read_tokens"] = cache_read_tokens
        if cache_write_tokens is not None:
            self._data["cache_write_tokens"] = cache_write_tokens
        if reasoning_tokens is not None:
            self._data["reasoning_tokens"] = reasoning_tokens
        if cost is not None:
            self._data["cost"] = cost
        if duration is not None:
            self._data["duration"] = duration
        if time_to_first_token is not None:
            self._data["time_to_first_token"] = time_to_first_token

    def to_dict(self):
        return dict(self._data)


class _FakeResponse:
    def __init__(self, content=None, metrics=None):
        self.content = content
        self.metrics = metrics


class _FakeAgent:
    def __init__(self, response):
        self._response = response
        self.received_prompt = None

    async def arun(self, prompt):
        self.received_prompt = prompt
        return self._response


def _make_agent_factory(response):
    agent = _FakeAgent(response)

    def factory(prompt):
        agent.received_prompt = prompt
        return agent

    return factory, agent


class TestTrialUsageModel(unittest.TestCase):

    def test_defaults(self):
        u = TrialUsage()
        self.assertIsNone(u.input_tokens)
        self.assertIsNone(u.output_tokens)
        self.assertIsNone(u.total_tokens)
        self.assertIsNone(u.cache_read_tokens)
        self.assertIsNone(u.cache_write_tokens)
        self.assertIsNone(u.reasoning_tokens)
        self.assertIsNone(u.cost)
        self.assertIsNone(u.duration)
        self.assertIsNone(u.time_to_first_token)
        self.assertFalse(u.usage_complete)

    def test_complete_when_all_required_present(self):
        u = TrialUsage(input_tokens=10, output_tokens=20, total_tokens=30)
        self.assertTrue(u.usage_complete)

    def test_incomplete_when_missing_output(self):
        u = TrialUsage(input_tokens=10, total_tokens=30)
        self.assertFalse(u.usage_complete)

    def test_incomplete_when_missing_input(self):
        u = TrialUsage(output_tokens=20, total_tokens=30)
        self.assertFalse(u.usage_complete)

    def test_complete_with_optional_fields(self):
        u = TrialUsage(
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            cache_read_tokens=5,
            reasoning_tokens=3,
            cost=0.001,
        )
        self.assertTrue(u.usage_complete)
        self.assertEqual(u.cache_read_tokens, 5)
        self.assertEqual(u.reasoning_tokens, 3)
        self.assertAlmostEqual(u.cost, 0.001)


class TestTrialResultModel(unittest.TestCase):

    def test_defaults(self):
        r = TrialResult()
        self.assertEqual(r.mode, "single_agent")
        self.assertEqual(r.status, "failed")
        self.assertIsNone(r.error)
        self.assertIsInstance(r.usage, TrialUsage)

    def test_trial_id_is_unique(self):
        r1 = TrialResult()
        r2 = TrialResult()
        self.assertNotEqual(r1.trial_id, r2.trial_id)


class TestHashText(unittest.TestCase):

    def test_deterministic(self):
        self.assertEqual(hash_text("abc"), hash_text("abc"))

    def test_different_inputs_differ(self):
        self.assertNotEqual(hash_text("abc"), hash_text("def"))


class TestParseDecision(unittest.TestCase):

    def test_from_incident_decision(self):
        d = _perfect_decision()
        result = parse_decision(d)
        self.assertIs(result, d)

    def test_from_dict(self):
        d = _perfect_decision()
        result = parse_decision(d.model_dump())
        self.assertIsInstance(result, IncidentDecision)
        self.assertEqual(result.root_cause_hypothesis_id, GROUND_TRUTH_HYPOTHESIS_ID)

    def test_from_basemodel(self):
        d = _perfect_decision()
        result = parse_decision(d)
        self.assertIsInstance(result, IncidentDecision)

    def test_from_strict_json_string(self):
        d = _perfect_decision()
        raw = json.dumps(d.model_dump())
        result = parse_decision(raw)
        self.assertIsInstance(result, IncidentDecision)
        self.assertEqual(result.root_cause_hypothesis_id, GROUND_TRUTH_HYPOTHESIS_ID)

    def test_invalid_json_string_raises(self):
        with self.assertRaises(ValueError):
            parse_decision("not json at all")

    def test_json_array_raises(self):
        with self.assertRaises(ValueError):
            parse_decision("[1, 2, 3]")

    def test_embedded_prose_not_mined(self):
        with self.assertRaises(ValueError):
            parse_decision("Here is the answer: {\"root_cause_hypothesis_id\": \"H01\"}")

    def test_unsupported_type_raises(self):
        with self.assertRaises(ValueError):
            parse_decision(42)


class TestExtractMetrics(unittest.TestCase):

    def test_none_returns_empty(self):
        u = extract_metrics(None)
        self.assertFalse(u.usage_complete)
        self.assertIsNone(u.input_tokens)

    def test_complete_metrics(self):
        m = _FakeMetrics(input_tokens=100, output_tokens=50, total_tokens=150)
        u = extract_metrics(m)
        self.assertTrue(u.usage_complete)
        self.assertEqual(u.input_tokens, 100)
        self.assertEqual(u.output_tokens, 50)
        self.assertEqual(u.total_tokens, 150)

    def test_missing_metrics_incomplete(self):
        m = _FakeMetrics(input_tokens=100)
        u = extract_metrics(m)
        self.assertFalse(u.usage_complete)
        self.assertEqual(u.input_tokens, 100)
        self.assertIsNone(u.output_tokens)

    def test_to_dict_failure_returns_empty(self):
        m = MagicMock()
        m.to_dict.side_effect = RuntimeError("boom")
        u = extract_metrics(m)
        self.assertFalse(u.usage_complete)

    def test_non_dict_to_dict_returns_empty(self):
        m = MagicMock()
        m.to_dict.return_value = "not a dict"
        u = extract_metrics(m)
        self.assertFalse(u.usage_complete)

    def test_agno_optional_fields_captured(self):
        m = _FakeMetrics(
            input_tokens=200,
            output_tokens=80,
            total_tokens=280,
            cache_read_tokens=40,
            cache_write_tokens=10,
            reasoning_tokens=15,
            cost=0.0025,
            duration=3.14,
            time_to_first_token=0.42,
        )
        u = extract_metrics(m)
        self.assertTrue(u.usage_complete)
        self.assertEqual(u.cache_read_tokens, 40)
        self.assertEqual(u.cache_write_tokens, 10)
        self.assertEqual(u.reasoning_tokens, 15)
        self.assertAlmostEqual(u.cost, 0.0025)
        self.assertAlmostEqual(u.duration, 3.14)
        self.assertAlmostEqual(u.time_to_first_token, 0.42)

    def test_optional_fields_none_when_absent(self):
        m = _FakeMetrics(input_tokens=10, output_tokens=20, total_tokens=30)
        u = extract_metrics(m)
        self.assertTrue(u.usage_complete)
        self.assertIsNone(u.cache_read_tokens)
        self.assertIsNone(u.cache_write_tokens)
        self.assertIsNone(u.reasoning_tokens)
        self.assertIsNone(u.cost)
        self.assertIsNone(u.duration)
        self.assertIsNone(u.time_to_first_token)


class TestRunSingleAgentSuccess(unittest.TestCase):

    def test_success_with_perfect_decision(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        metrics = _FakeMetrics(input_tokens=200, output_tokens=100, total_tokens=300)
        response = _FakeResponse(content=decision, metrics=metrics)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.provider, "qwen")
        self.assertEqual(result.model, "qwen3.7-plus")
        self.assertIsNotNone(result.parsed_decision)
        self.assertIsNotNone(result.evaluation)
        self.assertEqual(result.evaluation.total_score, 100.0)
        self.assertTrue(result.usage.usage_complete)
        self.assertEqual(result.usage.input_tokens, 200)

    def test_wall_duration_recorded(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        response = _FakeResponse(content=decision, metrics=_FakeMetrics())
        factory, _ = _make_agent_factory(response)

        t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 7, 13, 12, 0, 5, tzinfo=timezone.utc)
        calls = [t0, t1]
        call_idx = [0]

        def tick_clock():
            idx = min(call_idx[0], len(calls) - 1)
            call_idx[0] += 1
            return calls[idx]

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=tick_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertGreater(result.wall_duration_s, 0)
        self.assertTrue(result.started_at)
        self.assertTrue(result.finished_at)


class TestRunSingleAgentMetrics(unittest.TestCase):

    def test_complete_metrics_captured(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        metrics = _FakeMetrics(input_tokens=500, output_tokens=250, total_tokens=750)
        response = _FakeResponse(content=decision, metrics=metrics)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertTrue(result.usage.usage_complete)
        self.assertEqual(result.usage.input_tokens, 500)
        self.assertEqual(result.usage.output_tokens, 250)
        self.assertEqual(result.usage.total_tokens, 750)

    def test_missing_metrics_usage_incomplete(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        metrics = _FakeMetrics(input_tokens=100)
        response = _FakeResponse(content=decision, metrics=metrics)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertFalse(result.usage.usage_complete)
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertIsNone(result.usage.output_tokens)

    def test_no_metrics_object(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        response = _FakeResponse(content=decision, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertFalse(result.usage.usage_complete)


class TestRunSingleAgentParseFailure(unittest.TestCase):

    def test_strict_json_parse_failure(self):
        settings = _qwen_settings()
        response = _FakeResponse(content="not valid json", metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("parse failure", result.error)
        self.assertIsNone(result.parsed_decision)
        self.assertIsNone(result.evaluation)

    def test_embedded_prose_rejected(self):
        settings = _qwen_settings()
        raw = "The answer is: {\"root_cause_hypothesis_id\": \"H02\"}"
        response = _FakeResponse(content=raw, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("parse failure", result.error)


class TestRunSingleAgentTimeout(unittest.TestCase):

    def test_timeout_preserved_as_failure(self):
        settings = _qwen_settings()

        class _SlowAgent:
            async def arun(self, prompt):
                await asyncio.sleep(10)

        def slow_factory(prompt):
            return _SlowAgent()

        result = asyncio.run(
            run_single_agent(
                settings, agent_factory=slow_factory, clock=_fixed_clock, timeout_s=0.1
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("timeout", result.error)
        self.assertIsNone(result.parsed_decision)

    def test_exception_preserved(self):
        settings = _qwen_settings()

        class _BrokenAgent:
            async def arun(self, prompt):
                raise RuntimeError("provider exploded")

        def broken_factory(prompt):
            return _BrokenAgent()

        result = asyncio.run(
            run_single_agent(settings, agent_factory=broken_factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("RuntimeError", result.error)
        self.assertIn("provider exploded", result.error)


class TestPromptHashStability(unittest.TestCase):

    def test_prompt_hash_is_stable(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        response = _FakeResponse(content=decision, metrics=None)
        factory, _ = _make_agent_factory(response)

        r1 = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )
        r2 = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(r1.prompt_hash, r2.prompt_hash)
        self.assertEqual(r1.task_hash, r2.task_hash)
        self.assertTrue(len(r1.prompt_hash) > 0)
        self.assertTrue(len(r1.task_hash) > 0)


class TestEvaluatorIntegration(unittest.TestCase):

    def test_evaluation_result_attached(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        response = _FakeResponse(content=decision, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertIsNotNone(result.evaluation)
        self.assertEqual(result.evaluation.mode, "single_agent")
        self.assertEqual(result.evaluation.total_score, 100.0)

    def test_imperfect_decision_evaluated(self):
        settings = _qwen_settings()
        decision = IncidentDecision(
            root_cause_hypothesis_id="H01",
            hypothesis_evidence=["F01"],
            recommended_actions=["A01"],
            recommended_controls=["C01"],
            evidence_citations={},
            stakeholder_constraints_acknowledged=[],
        )
        response = _FakeResponse(content=decision, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertIsNotNone(result.evaluation)
        self.assertLess(result.evaluation.total_score, 100.0)
        self.assertGreaterEqual(result.evaluation.total_score, 0.0)


class TestProviderGuard(unittest.TestCase):

    def test_non_qwen_provider_rejected_without_factory(self):
        settings = Settings(
            LLM_PROVIDER="cerebras",
            CEREBRAS_API_KEY="test-key",
        )

        result = asyncio.run(
            run_single_agent(settings, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("qwen", result.error)

    def test_wrong_qwen_model_rejected_without_factory(self):
        settings = Settings(
            LLM_PROVIDER="qwen",
            QWEN_API_KEY="test-key",
            QWEN_MODEL="qwen3.6-flash",
        )

        result = asyncio.run(
            run_single_agent(settings, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("qwen3.7-plus", result.error)


class TestRawOutputSerialization(unittest.TestCase):

    def test_basemodel_content_serialized_to_json_string(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        response = _FakeResponse(content=decision, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertIsInstance(result.raw_output, str)
        parsed_back = json.loads(result.raw_output)
        self.assertIsInstance(parsed_back, dict)
        self.assertEqual(
            parsed_back["root_cause_hypothesis_id"], GROUND_TRUTH_HYPOTHESIS_ID
        )

    def test_dict_content_serialized_to_json_string(self):
        settings = _qwen_settings()
        decision_dict = _perfect_decision().model_dump()
        response = _FakeResponse(content=decision_dict, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertIsInstance(result.raw_output, str)
        parsed_back = json.loads(result.raw_output)
        self.assertIsInstance(parsed_back, dict)
        self.assertEqual(
            parsed_back["root_cause_hypothesis_id"], GROUND_TRUTH_HYPOTHESIS_ID
        )

    def test_string_content_preserved_exactly(self):
        settings = _qwen_settings()
        raw_json = json.dumps(_perfect_decision().model_dump())
        response = _FakeResponse(content=raw_json, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertIsInstance(result.raw_output, str)
        self.assertEqual(result.raw_output, raw_json)

    def test_non_basemodel_content_serialized(self):
        settings = _qwen_settings()

        class _CustomModel(BaseModel):
            root_cause_hypothesis_id: str = "H02"
            hypothesis_evidence: list[str] = []
            recommended_actions: list[str] = []
            recommended_controls: list[str] = []
            evidence_citations: dict[str, list[str]] = {}
            stakeholder_constraints_acknowledged: list[str] = []
            governance_activity: list[str] = []

        custom = _CustomModel()
        response = _FakeResponse(content=custom, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertIsInstance(result.raw_output, str)
        self.assertNotIsInstance(result.raw_output, BaseModel)
        parsed_back = json.loads(result.raw_output)
        self.assertEqual(parsed_back["root_cause_hypothesis_id"], "H02")

    def test_raw_output_is_never_basemodel(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        response = _FakeResponse(content=decision, metrics=None)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertIsNotNone(result.raw_output)
        self.assertIsInstance(result.raw_output, str)


class TestAgnoMetricExtraction(unittest.TestCase):

    def test_token_budget_overrun_is_preserved_as_failure(self):
        settings = _qwen_settings()
        metrics = _FakeMetrics(input_tokens=300, output_tokens=150, total_tokens=450)
        response = _FakeResponse(content=_perfect_decision(), metrics=metrics)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(run_single_agent(
            settings,
            agent_factory=factory,
            clock=_fixed_clock,
            max_total_tokens=449,
        ))

        self.assertEqual(result.status, "failed")
        self.assertIn("token budget exceeded", result.error)

    def test_full_agno_metrics_in_trial(self):
        settings = _qwen_settings()
        decision = _perfect_decision()
        metrics = _FakeMetrics(
            input_tokens=300,
            output_tokens=150,
            total_tokens=450,
            cache_read_tokens=60,
            cache_write_tokens=20,
            reasoning_tokens=25,
            cost=0.005,
            duration=2.5,
            time_to_first_token=0.3,
        )
        response = _FakeResponse(content=decision, metrics=metrics)
        factory, _ = _make_agent_factory(response)

        result = asyncio.run(
            run_single_agent(settings, agent_factory=factory, clock=_fixed_clock)
        )

        self.assertEqual(result.status, "success")
        self.assertTrue(result.usage.usage_complete)
        self.assertEqual(result.usage.input_tokens, 300)
        self.assertEqual(result.usage.output_tokens, 150)
        self.assertEqual(result.usage.total_tokens, 450)
        self.assertEqual(result.usage.cache_read_tokens, 60)
        self.assertEqual(result.usage.cache_write_tokens, 20)
        self.assertEqual(result.usage.reasoning_tokens, 25)
        self.assertAlmostEqual(result.usage.cost, 0.005)
        self.assertAlmostEqual(result.usage.duration, 2.5)
        self.assertAlmostEqual(result.usage.time_to_first_token, 0.3)


if __name__ == "__main__":
    unittest.main()
