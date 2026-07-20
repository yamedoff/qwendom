from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks import (
    CheckResult,
    EvaluationResult,
    IncidentDecision,
    evaluate,
    load_ground_truth,
)
from benchmarks.evaluator import (
    PENALTY_PER_INVALID_ID,
    POINTS_ACTION_ORDER,
    POINTS_ACTION_SET,
    POINTS_CONSTRAINTS,
    POINTS_CONTROLS,
    POINTS_EVIDENCE_NO_UNSUPPORTED,
    POINTS_EVIDENCE_REQUIRED_CLAIMS,
    POINTS_EVIDENCE_VALID_IDS,
    POINTS_HYPOTHESIS_EVIDENCE,
    POINTS_HYPOTHESIS_ID,
)
from benchmarks.fixtures.ground_truth import (
    GROUND_TRUTH_ACTION_ORDER,
    GROUND_TRUTH_ACTIONS,
    GROUND_TRUTH_CONTROLS,
    GROUND_TRUTH_CONSTRAINT_SATISFACTION,
    GROUND_TRUTH_HYPOTHESIS_EVIDENCE,
    GROUND_TRUTH_HYPOTHESIS_ID,
    GROUND_TRUTH_REQUIRED_CLAIMS,
    VALID_FACT_IDS,
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


class TestRubricConstants(unittest.TestCase):

    def test_sub_constants_sum_to_100(self) -> None:
        total = (
            POINTS_HYPOTHESIS_ID
            + POINTS_HYPOTHESIS_EVIDENCE
            + POINTS_ACTION_SET
            + POINTS_ACTION_ORDER
            + POINTS_CONTROLS
            + POINTS_EVIDENCE_VALID_IDS
            + POINTS_EVIDENCE_REQUIRED_CLAIMS
            + POINTS_EVIDENCE_NO_UNSUPPORTED
            + POINTS_CONSTRAINTS
        )
        self.assertEqual(total, 100.0)


class TestGroundTruthPerfectScore(unittest.TestCase):

    def test_perfect_decision_scores_100(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertEqual(result.total_score, 100.0)

    def test_perfect_decision_all_checks_pass(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        for check in result.per_check_results:
            self.assertTrue(check.passed, f"Check {check.check_id} should pass")

    def test_perfect_decision_no_penalties(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertEqual(result.penalty_total, 0.0)
        self.assertEqual(len(result.penalties), 0)

    def test_perfect_decision_governance_zero(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertEqual(result.governance_score, 0.0)

    def test_perfect_decision_category_scores(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertEqual(result.root_cause_score, POINTS_HYPOTHESIS_ID + POINTS_HYPOTHESIS_EVIDENCE)
        self.assertEqual(result.actions_score, POINTS_ACTION_SET + POINTS_ACTION_ORDER)
        self.assertEqual(result.controls_score, POINTS_CONTROLS)
        self.assertEqual(result.evidence_score, POINTS_EVIDENCE_VALID_IDS + POINTS_EVIDENCE_REQUIRED_CLAIMS + POINTS_EVIDENCE_NO_UNSUPPORTED)
        self.assertEqual(result.constraints_score, POINTS_CONSTRAINTS)


class TestEmptyDecision(unittest.TestCase):

    def test_empty_decision_scores_low(self) -> None:
        decision = IncidentDecision(
            root_cause_hypothesis_id="H99",
            hypothesis_evidence=[],
            recommended_actions=[],
            recommended_controls=[],
            evidence_citations={},
            stakeholder_constraints_acknowledged=[],
            governance_activity=[],
        )
        result = evaluate(decision)
        self.assertLess(result.total_score, 20.0)

    def test_empty_decision_hypothesis_id_fails(self) -> None:
        decision = IncidentDecision(
            root_cause_hypothesis_id="H99",
        )
        result = evaluate(decision)
        rc_id_check = next(c for c in result.per_check_results if c.check_id == "RC_HYPOTHESIS_ID")
        self.assertFalse(rc_id_check.passed)
        self.assertEqual(rc_id_check.points_awarded, 0.0)


class TestMostlyWrongDecision(unittest.TestCase):

    def test_mostly_wrong_scores_low(self) -> None:
        decision = IncidentDecision(
            root_cause_hypothesis_id="H01",
            hypothesis_evidence=["F01", "F02"],
            recommended_actions=["A04", "A05"],
            recommended_controls=["C01", "C05"],
            evidence_citations={"deployment_cause": ["F01"]},
            stakeholder_constraints_acknowledged=["SC01"],
            governance_activity=[],
        )
        result = evaluate(decision)
        self.assertLess(result.total_score, 50.0)


class TestWrongRootCause(unittest.TestCase):

    def test_wrong_hypothesis_id_zero_points(self) -> None:
        decision = _perfect_decision()
        decision.root_cause_hypothesis_id = "H01"
        result = evaluate(decision)
        rc_id_check = next(c for c in result.per_check_results if c.check_id == "RC_HYPOTHESIS_ID")
        self.assertFalse(rc_id_check.passed)
        self.assertEqual(rc_id_check.points_awarded, 0.0)

    def test_wrong_hypothesis_loses_15_points(self) -> None:
        perfect = _perfect_decision()
        wrong = _perfect_decision()
        wrong.root_cause_hypothesis_id = "H03"
        perfect_result = evaluate(perfect)
        wrong_result = evaluate(wrong)
        diff = perfect_result.total_score - wrong_result.total_score
        self.assertAlmostEqual(diff, POINTS_HYPOTHESIS_ID, places=2)


class TestActionOrderPenalty(unittest.TestCase):

    def test_reversed_order_loses_points(self) -> None:
        decision = _perfect_decision()
        decision.recommended_actions = list(reversed(GROUND_TRUTH_ACTION_ORDER))
        result = evaluate(decision)
        order_check = next(c for c in result.per_check_results if c.check_id == "ACT_ORDER")
        self.assertFalse(order_check.passed)
        self.assertLess(order_check.points_awarded, POINTS_ACTION_ORDER)

    def test_correct_set_still_gets_set_points(self) -> None:
        decision = _perfect_decision()
        decision.recommended_actions = list(reversed(GROUND_TRUTH_ACTION_ORDER))
        result = evaluate(decision)
        set_check = next(c for c in result.per_check_results if c.check_id == "ACT_SET")
        self.assertTrue(set_check.passed)
        self.assertEqual(set_check.points_awarded, POINTS_ACTION_SET)

    def test_wrong_set_loses_order_points(self) -> None:
        decision = _perfect_decision()
        decision.recommended_actions = ["A04", "A05"]
        result = evaluate(decision)
        set_check = next(c for c in result.per_check_results if c.check_id == "ACT_SET")
        self.assertFalse(set_check.passed)


class TestConstraintPenalty(unittest.TestCase):

    def test_missing_all_constraints(self) -> None:
        decision = _perfect_decision()
        decision.stakeholder_constraints_acknowledged = []
        result = evaluate(decision)
        con_check = next(c for c in result.per_check_results if c.check_id == "CON_SET")
        self.assertFalse(con_check.passed)
        self.assertEqual(con_check.points_awarded, 0.0)

    def test_partial_constraints_partial_points(self) -> None:
        decision = _perfect_decision()
        decision.stakeholder_constraints_acknowledged = ["SC01"]
        result = evaluate(decision)
        con_check = next(c for c in result.per_check_results if c.check_id == "CON_SET")
        self.assertFalse(con_check.passed)
        self.assertGreater(con_check.points_awarded, 0.0)
        self.assertLess(con_check.points_awarded, POINTS_CONSTRAINTS)


class TestInvalidEvidence(unittest.TestCase):

    def test_invalid_fact_id_in_hypothesis_evidence(self) -> None:
        decision = _perfect_decision()
        decision.hypothesis_evidence.append("F99")
        result = evaluate(decision)
        self.assertGreater(result.penalty_total, 0.0)
        self.assertTrue(any("F99" in p for p in result.penalties))

    def test_invalid_fact_id_in_evidence_citations(self) -> None:
        decision = _perfect_decision()
        decision.evidence_citations["deployment_cause"].append("FZZ")
        result = evaluate(decision)
        self.assertGreater(result.penalty_total, 0.0)
        self.assertTrue(any("FZZ" in p for p in result.penalties))

    def test_penalty_amount_per_invalid_id(self) -> None:
        decision = _perfect_decision()
        decision.hypothesis_evidence.extend(["F88", "F77"])
        result = evaluate(decision)
        self.assertAlmostEqual(result.penalty_total, PENALTY_PER_INVALID_ID * 2, places=2)


class TestUnsupportedIDs(unittest.TestCase):

    def test_unsupported_hypothesis_id_scores_zero(self) -> None:
        decision = _perfect_decision()
        decision.root_cause_hypothesis_id = "H99"
        result = evaluate(decision)
        rc_check = next(c for c in result.per_check_results if c.check_id == "RC_HYPOTHESIS_ID")
        self.assertEqual(rc_check.points_awarded, 0.0)

    def test_unsupported_action_ids_reduce_jaccard(self) -> None:
        decision = _perfect_decision()
        decision.recommended_actions = ["A99"]
        result = evaluate(decision)
        set_check = next(c for c in result.per_check_results if c.check_id == "ACT_SET")
        self.assertEqual(set_check.points_awarded, 0.0)

    def test_unsupported_control_ids_reduce_jaccard(self) -> None:
        decision = _perfect_decision()
        decision.recommended_controls = ["C99"]
        result = evaluate(decision)
        ctrl_check = next(c for c in result.per_check_results if c.check_id == "CTRL_SET")
        self.assertEqual(ctrl_check.points_awarded, 0.0)


class TestDeterministicRepeatability(unittest.TestCase):

    def test_same_input_same_output(self) -> None:
        decision = _perfect_decision()
        r1 = evaluate(decision)
        r2 = evaluate(decision)
        self.assertEqual(r1.total_score, r2.total_score)
        self.assertEqual(r1.per_check_results, r2.per_check_results)
        self.assertEqual(r1.penalties, r2.penalties)

    def test_imperfect_decision_deterministic(self) -> None:
        decision = IncidentDecision(
            root_cause_hypothesis_id="H01",
            hypothesis_evidence=["F01", "F99"],
            recommended_actions=["A04"],
            recommended_controls=["C01"],
            evidence_citations={"deployment_cause": ["F01", "FXX"]},
            stakeholder_constraints_acknowledged=["SC01"],
            governance_activity=["reviewed"],
        )
        r1 = evaluate(decision)
        r2 = evaluate(decision)
        r3 = evaluate(decision)
        self.assertEqual(r1.total_score, r2.total_score)
        self.assertEqual(r2.total_score, r3.total_score)
        self.assertEqual(r1.penalty_total, r2.penalty_total)


class TestModeNeutrality(unittest.TestCase):

    def test_single_agent_and_society_identical_score(self) -> None:
        decision = _perfect_decision()
        r_single = evaluate(decision, mode="single_agent")
        r_society = evaluate(decision, mode="society")
        self.assertEqual(r_single.total_score, r_society.total_score)

    def test_mode_labels_differ_only_in_mode_field(self) -> None:
        decision = _perfect_decision()
        r_single = evaluate(decision, mode="single_agent")
        r_society = evaluate(decision, mode="society")
        self.assertEqual(r_single.mode, "single_agent")
        self.assertEqual(r_society.mode, "society")
        self.assertEqual(r_single.per_check_results, r_society.per_check_results)
        self.assertEqual(r_single.penalties, r_society.penalties)
        self.assertEqual(r_single.penalty_total, r_society.penalty_total)

    def test_imperfect_decision_mode_neutral(self) -> None:
        decision = IncidentDecision(
            root_cause_hypothesis_id="H03",
            hypothesis_evidence=["F01", "F14"],
            recommended_actions=["A02"],
            recommended_controls=["C03"],
            evidence_citations={},
            stakeholder_constraints_acknowledged=[],
            governance_activity=["step1", "step2"],
        )
        r_single = evaluate(decision, mode="single_agent")
        r_society = evaluate(decision, mode="society")
        self.assertEqual(r_single.total_score, r_society.total_score)
        self.assertEqual(r_single.per_check_results, r_society.per_check_results)


class TestGovernanceExclusion(unittest.TestCase):

    def test_governance_activity_does_not_affect_score(self) -> None:
        d1 = _perfect_decision()
        d1.governance_activity = []
        d2 = _perfect_decision()
        d2.governance_activity = ["reviewed by lead", "approved by CISO", "audit logged"]
        r1 = evaluate(d1)
        r2 = evaluate(d2)
        self.assertEqual(r1.total_score, r2.total_score)
        self.assertEqual(r1.governance_score, 0.0)
        self.assertEqual(r2.governance_score, 0.0)


class TestScoreClamping(unittest.TestCase):

    def test_score_never_below_zero(self) -> None:
        decision = IncidentDecision(
            root_cause_hypothesis_id="H99",
            hypothesis_evidence=["F88", "F77", "F66", "F55"],
            recommended_actions=["A99"],
            recommended_controls=["C99"],
            evidence_citations={"x": ["F44", "F33"]},
            stakeholder_constraints_acknowledged=[],
        )
        result = evaluate(decision)
        self.assertGreaterEqual(result.total_score, 0.0)

    def test_score_never_above_100(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertLessEqual(result.total_score, 100.0)


class TestLoadGroundTruth(unittest.TestCase):

    def test_load_ground_truth_returns_dict(self) -> None:
        gt = load_ground_truth()
        self.assertIsInstance(gt, dict)
        self.assertEqual(gt["hypothesis_id"], GROUND_TRUTH_HYPOTHESIS_ID)

    def test_valid_fact_ids_count(self) -> None:
        self.assertEqual(len(VALID_FACT_IDS), 25)


class TestEvaluationResultSchema(unittest.TestCase):

    def test_result_is_evaluation_result(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertIsInstance(result, EvaluationResult)

    def test_per_check_results_are_check_result(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        for check in result.per_check_results:
            self.assertIsInstance(check, CheckResult)

    def test_metadata_contains_rubric_version(self) -> None:
        decision = _perfect_decision()
        result = evaluate(decision)
        self.assertIn("rubric_version", result.metadata)


if __name__ == "__main__":
    unittest.main()
