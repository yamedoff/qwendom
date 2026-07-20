"""Pure deterministic evaluator with an identical 100-point rubric.

Scoring breakdown (100 points total):
  - Root cause hypothesis: 25 pts  (ID 15 + evidence 10)
  - Action selection:       25 pts  (set 15 + order 10)
  - Control selection:      15 pts
  - Evidence integrity:     20 pts  (valid IDs 5 + required claims 10 + no unsupported 5)
  - Constraint compliance:  15 pts
  - Governance activity:     0 pts  (explicitly excluded)

Penalties:
  - Each unsupported / invalid fact ID cited: −3 pts from total.

Final score is clamped to [0, 100].
"""

from __future__ import annotations

from typing import Any

from benchmarks import CheckResult, EvaluationResult, IncidentDecision, Mode
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

POINTS_ROOT_CAUSE: float = 25.0
POINTS_HYPOTHESIS_ID: float = 15.0
POINTS_HYPOTHESIS_EVIDENCE: float = 10.0

POINTS_ACTIONS: float = 25.0
POINTS_ACTION_SET: float = 15.0
POINTS_ACTION_ORDER: float = 10.0

POINTS_CONTROLS: float = 15.0

POINTS_EVIDENCE: float = 20.0
POINTS_EVIDENCE_VALID_IDS: float = 5.0
POINTS_EVIDENCE_REQUIRED_CLAIMS: float = 10.0
POINTS_EVIDENCE_NO_UNSUPPORTED: float = 5.0

POINTS_CONSTRAINTS: float = 15.0

PENALTY_PER_INVALID_ID: float = 3.0


def evaluate(decision: IncidentDecision, mode: Mode = "single_agent") -> EvaluationResult:
    """Score an IncidentDecision against the ground truth.

    The ``mode`` parameter is recorded in the result but does **not** alter
    the rubric or any comparison logic.

    Args:
        decision: The structured decision to evaluate.
        mode: ``"single_agent"`` or ``"society"``.

    Returns:
        A fully populated :class:`EvaluationResult`.
    """
    checks: list[CheckResult] = []
    penalties: list[str] = []

    rc_checks, rc_score = _score_root_cause(decision)
    checks.extend(rc_checks)

    act_checks, act_score = _score_actions(decision)
    checks.extend(act_checks)

    ctrl_checks, ctrl_score = _score_controls(decision)
    checks.extend(ctrl_checks)

    ev_checks, ev_score, ev_penalties = _score_evidence(decision)
    checks.extend(ev_checks)
    penalties.extend(ev_penalties)

    con_checks, con_score = _score_constraints(decision)
    checks.extend(con_checks)

    raw_total = rc_score + act_score + ctrl_score + ev_score + con_score

    id_penalties, id_penalty_total = _penalise_invalid_ids(decision)
    penalties.extend(id_penalties)

    final = max(0.0, min(100.0, raw_total - id_penalty_total))

    return EvaluationResult(
        mode=mode,
        total_score=round(final, 4),
        root_cause_score=round(rc_score, 4),
        actions_score=round(act_score, 4),
        controls_score=round(ctrl_score, 4),
        evidence_score=round(ev_score, 4),
        constraints_score=round(con_score, 4),
        governance_score=0.0,
        per_check_results=checks,
        penalties=penalties,
        penalty_total=round(id_penalty_total, 4),
        metadata={
            "raw_total": round(raw_total, 4),
            "rubric_version": "slice1-v1",
        },
    )


def _score_root_cause(decision: IncidentDecision) -> tuple[list[CheckResult], float]:
    checks: list[CheckResult] = []
    score: float = 0.0

    hid_match = decision.root_cause_hypothesis_id == GROUND_TRUTH_HYPOTHESIS_ID
    hid_pts = POINTS_HYPOTHESIS_ID if hid_match else 0.0
    score += hid_pts
    checks.append(CheckResult(
        check_id="RC_HYPOTHESIS_ID",
        category="root_cause",
        passed=hid_match,
        points_awarded=hid_pts,
        points_possible=POINTS_HYPOTHESIS_ID,
        detail=f"expected={GROUND_TRUTH_HYPOTHESIS_ID} got={decision.root_cause_hypothesis_id}",
    ))

    got_evidence = set(decision.hypothesis_evidence)
    required_evidence = set(GROUND_TRUTH_HYPOTHESIS_EVIDENCE)
    invalid_in_evidence = got_evidence - VALID_FACT_IDS
    valid_got = got_evidence & VALID_FACT_IDS
    if required_evidence:
        recall = len(valid_got & required_evidence) / len(required_evidence)
    else:
        recall = 1.0
    precision = len(valid_got & required_evidence) / len(valid_got) if valid_got else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    ev_pts = POINTS_HYPOTHESIS_EVIDENCE * f1
    score += ev_pts
    checks.append(CheckResult(
        check_id="RC_HYPOTHESIS_EVIDENCE",
        category="root_cause",
        passed=f1 >= 0.999,
        points_awarded=round(ev_pts, 4),
        points_possible=POINTS_HYPOTHESIS_EVIDENCE,
        detail=f"f1={f1:.4f} expected_ids={sorted(required_evidence)} got_ids={sorted(got_evidence)} invalid={sorted(invalid_in_evidence)}",
    ))

    return checks, score


def _score_actions(decision: IncidentDecision) -> tuple[list[CheckResult], float]:
    checks: list[CheckResult] = []
    score: float = 0.0

    got_set = set(decision.recommended_actions)
    required_set = set(GROUND_TRUTH_ACTIONS)
    if required_set:
        jaccard = len(got_set & required_set) / len(got_set | required_set)
    else:
        jaccard = 1.0 if not got_set else 0.0
    set_pts = POINTS_ACTION_SET * jaccard
    score += set_pts
    checks.append(CheckResult(
        check_id="ACT_SET",
        category="actions",
        passed=jaccard >= 0.999,
        points_awarded=round(set_pts, 4),
        points_possible=POINTS_ACTION_SET,
        detail=f"jaccard={jaccard:.4f} expected={sorted(required_set)} got={sorted(got_set)}",
    ))

    got_order = [a for a in decision.recommended_actions if a in required_set]
    expected_order = GROUND_TRUTH_ACTION_ORDER
    if len(got_order) == len(expected_order) and got_order == expected_order:
        order_pts = POINTS_ACTION_ORDER
        order_passed = True
    elif len(got_order) == 0 and len(expected_order) == 0:
        order_pts = POINTS_ACTION_ORDER
        order_passed = True
    else:
        matched = sum(1 for a, b in zip(got_order, expected_order) if a == b)
        denom = max(len(got_order), len(expected_order))
        order_pts = POINTS_ACTION_ORDER * (matched / denom) if denom else 0.0
        order_passed = False
    score += order_pts
    checks.append(CheckResult(
        check_id="ACT_ORDER",
        category="actions",
        passed=order_passed,
        points_awarded=round(order_pts, 4),
        points_possible=POINTS_ACTION_ORDER,
        detail=f"expected_order={expected_order} got_filtered={got_order}",
    ))

    return checks, score


def _score_controls(decision: IncidentDecision) -> tuple[list[CheckResult], float]:
    checks: list[CheckResult] = []

    got_set = set(decision.recommended_controls)
    required_set = set(GROUND_TRUTH_CONTROLS)
    if required_set:
        jaccard = len(got_set & required_set) / len(got_set | required_set)
    else:
        jaccard = 1.0 if not got_set else 0.0
    pts = POINTS_CONTROLS * jaccard
    checks.append(CheckResult(
        check_id="CTRL_SET",
        category="controls",
        passed=jaccard >= 0.999,
        points_awarded=round(pts, 4),
        points_possible=POINTS_CONTROLS,
        detail=f"jaccard={jaccard:.4f} expected={sorted(required_set)} got={sorted(got_set)}",
    ))

    return checks, pts


def _score_evidence(
    decision: IncidentDecision,
) -> tuple[list[CheckResult], float, list[str]]:
    checks: list[CheckResult] = []
    penalties: list[str] = []
    score: float = 0.0

    all_cited_ids: set[str] = set()
    for ids in decision.evidence_citations.values():
        all_cited_ids.update(ids)
    all_cited_ids.update(decision.hypothesis_evidence)

    invalid_ids = all_cited_ids - VALID_FACT_IDS
    valid_ratio = 1.0 - (len(invalid_ids) / len(all_cited_ids)) if all_cited_ids else 1.0
    valid_pts = POINTS_EVIDENCE_VALID_IDS * valid_ratio
    score += valid_pts
    checks.append(CheckResult(
        check_id="EV_VALID_IDS",
        category="evidence",
        passed=len(invalid_ids) == 0,
        points_awarded=round(valid_pts, 4),
        points_possible=POINTS_EVIDENCE_VALID_IDS,
        detail=f"invalid_ids={sorted(invalid_ids)} valid_ratio={valid_ratio:.4f}",
    ))

    required_claims = GROUND_TRUTH_REQUIRED_CLAIMS
    claims_satisfied = 0
    total_claims = len(required_claims)
    for claim, required_fids in required_claims.items():
        cited_for_claim = set(decision.evidence_citations.get(claim, []))
        if set(required_fids).issubset(cited_for_claim):
            claims_satisfied += 1
    claim_ratio = claims_satisfied / total_claims if total_claims else 1.0
    claim_pts = POINTS_EVIDENCE_REQUIRED_CLAIMS * claim_ratio
    score += claim_pts
    checks.append(CheckResult(
        check_id="EV_REQUIRED_CLAIMS",
        category="evidence",
        passed=claim_ratio >= 0.999,
        points_awarded=round(claim_pts, 4),
        points_possible=POINTS_EVIDENCE_REQUIRED_CLAIMS,
        detail=f"satisfied={claims_satisfied}/{total_claims}",
    ))

    unsupported = all_cited_ids - VALID_FACT_IDS
    if unsupported:
        no_unsupported_pts = 0.0
    else:
        no_unsupported_pts = POINTS_EVIDENCE_NO_UNSUPPORTED
    score += no_unsupported_pts
    checks.append(CheckResult(
        check_id="EV_NO_UNSUPPORTED",
        category="evidence",
        passed=len(unsupported) == 0,
        points_awarded=no_unsupported_pts,
        points_possible=POINTS_EVIDENCE_NO_UNSUPPORTED,
        detail=f"unsupported_count={len(unsupported)}",
    ))

    return checks, score, penalties


def _score_constraints(decision: IncidentDecision) -> tuple[list[CheckResult], float]:
    checks: list[CheckResult] = []

    required = set(GROUND_TRUTH_CONSTRAINT_SATISFACTION.keys())
    acknowledged = set(decision.stakeholder_constraints_acknowledged)
    if required:
        jaccard = len(acknowledged & required) / len(acknowledged | required)
    else:
        jaccard = 1.0 if not acknowledged else 0.0
    pts = POINTS_CONSTRAINTS * jaccard
    checks.append(CheckResult(
        check_id="CON_SET",
        category="constraints",
        passed=jaccard >= 0.999,
        points_awarded=round(pts, 4),
        points_possible=POINTS_CONSTRAINTS,
        detail=f"jaccard={jaccard:.4f} expected={sorted(required)} got={sorted(acknowledged)}",
    ))

    return checks, pts


def _penalise_invalid_ids(
    decision: IncidentDecision,
) -> tuple[list[str], float]:
    penalties: list[str] = []
    total: float = 0.0

    all_ids: set[str] = set()
    all_ids.update(decision.hypothesis_evidence)
    for ids in decision.evidence_citations.values():
        all_ids.update(ids)

    invalid = sorted(all_ids - VALID_FACT_IDS)
    for fid in invalid:
        penalties.append(f"invalid_fact_id_cited:{fid}")
        total += PENALTY_PER_INVALID_ID

    return penalties, total
