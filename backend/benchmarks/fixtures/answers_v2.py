"""Private answer keys for suite v2; never import this from prompt code."""

from benchmarks.suite_v2 import TaskAnswerKey


ANSWER_KEYS: dict[str, TaskAnswerKey] = {
    "incident-diagnosis": TaskAnswerKey(
        selected_ids={"H02", "A01", "A02"}, ordered_ids=["A01", "A02"],
        evidence_citations={
            "deployment_cause": {"F01", "F02", "F07"},
            "connection_exhaustion": {"F02", "F03", "F04"},
            "service_impact": {"F05"},
        }, constraint_ids={"C01", "C02"},
    ),
    "plan-prioritization": TaskAnswerKey(
        selected_ids={"W01", "W02", "W03"}, ordered_ids=["W01", "W02", "W03"],
        evidence_citations={
            "blocker_first": {"P01", "P05"},
            "dependency_order": {"P02", "P03", "P07"},
            "scope_control": {"P04", "P06"},
        }, constraint_ids={"C01", "C02"}, numeric_answers={"minimum_minutes": 45},
    ),
    "evidence-verification": TaskAnswerKey(
        selected_ids={"CL01", "CL02", "CL04"},
        evidence_citations={
            "restart_proof": {"E02"}, "provider_proof": {"E04"},
            "test_proof": {"E01"}, "deployment_gap": {"E03", "E05"},
        },
    ),
    "constraint-aware-decision": TaskAnswerKey(
        selected_ids={"O2"},
        evidence_citations={
            "budget_eligibility": {"D02", "D04"},
            "deadline_eligibility": {"D02", "D05"},
            "quality_selection": {"D01", "D02", "D03"},
        }, constraint_ids={"C01", "C02"},
        numeric_answers={"selected_quality": 88, "selected_cost": 80, "selected_days": 2},
    ),
}
