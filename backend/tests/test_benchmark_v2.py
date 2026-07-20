from __future__ import annotations

import sys
import inspect
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.evaluator_v2 import evaluate_task, macro_average
from benchmarks.loader_v2 import build_task_prompt
import benchmarks.loader_v2 as loader_v2
from benchmarks.fixtures.answers_v2 import ANSWER_KEYS
from benchmarks.suite_v2 import BenchmarkAnswer, TASKS
from benchmarks.tools_v2 import TOOL_NAMES, calculate, lookup_dataset, lookup_record


class BenchmarkV2Tests(unittest.TestCase):
    def perfect(self, task_id: str) -> BenchmarkAnswer:
        key = ANSWER_KEYS[task_id]
        return BenchmarkAnswer(
            selected_ids=sorted(key.selected_ids), ordered_ids=key.ordered_ids,
            evidence_citations={label: sorted(ids) for label, ids in key.evidence_citations.items()},
            constraint_ids=sorted(key.constraint_ids), numeric_answers=key.numeric_answers,
        )

    def test_suite_contains_four_distinct_ask_types(self):
        self.assertEqual(len(TASKS), 4)
        self.assertEqual(len({task.kind for task in TASKS.values()}), 4)

    def test_public_prompts_reveal_labels_but_not_private_key(self):
        self.assertNotIn("answers_v2", inspect.getsource(loader_v2))
        for task_id, task in TASKS.items():
            prompt = build_task_prompt(task_id)
            for label in task.required_claim_labels:
                self.assertIn(label, prompt)
            self.assertNotIn("ANSWER_KEYS", prompt)

    def test_perfect_answers_score_100_and_macro_is_equal_weight(self):
        results = [evaluate_task(task_id, self.perfect(task_id)) for task_id in TASKS]
        self.assertTrue(all(result["score"] == 100 for result in results))
        self.assertEqual(macro_average(results), 100)

    def test_governance_is_not_scored(self):
        answer = self.perfect("evidence-verification")
        original = evaluate_task("evidence-verification", answer)["score"]
        answer.governance_activity = ["held 500 meetings"]
        self.assertEqual(evaluate_task("evidence-verification", answer)["score"], original)

    def test_incomplete_macro_suite_is_rejected(self):
        with self.assertRaises(ValueError):
            macro_average([evaluate_task("incident-diagnosis", self.perfect("incident-diagnosis"))])

    def test_tools_are_read_only_and_arithmetic_is_restricted(self):
        self.assertEqual(TOOL_NAMES, ("lookup_record", "lookup_dataset", "calculate"))
        self.assertEqual(lookup_record("incident-diagnosis", "F01")["record_id"], "F01")
        self.assertEqual(len(lookup_dataset("incident-diagnosis", ["F01", "F02"])), 2)
        invalid = lookup_record("evidence-verification", "CL01")
        self.assertFalse(invalid["found"])
        self.assertEqual(invalid["error"], "unknown_record_id")
        self.assertIn("E01", invalid["valid_record_ids"])
        self.assertEqual(calculate("(50 * 8) + 75"), 475)
        with self.assertRaises(ValueError):
            calculate("__import__('os').getcwd()")
        with self.assertRaises(ValueError):
            calculate("999999999 * 999999999")


if __name__ == "__main__":
    unittest.main()
