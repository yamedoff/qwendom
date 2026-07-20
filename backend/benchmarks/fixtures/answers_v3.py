"""Private deterministic key for benchmark v3; never import from prompt code."""

EXPECTED_FINDINGS = {"V01", "V02", "V03", "V04", "V05", "V06", "V07"}
EXPECTED_STAGES = ["G01", "G02", "G03", "G04"]
EXPECTED_EVIDENCE = {
    "authz_blocker": {"S01"},
    "webhook_blockers": {"S06", "S07", "S09"},
    "supply_chain_blockers": {"S11", "S12"},
    "test_gap": {"S16"},
    "release_order": {"S20", "S21", "S22", "S23"},
    "rollback_constraint": {"S17", "S23"},
    "parallel_timing": {"S19", "S24"},
}
EXPECTED_CONSTRAINTS = {"C01", "C02", "C03"}
EXPECTED_NUMERICS = {"release_blockers": 7.0, "critical_findings": 6.0, "minimum_minutes": 57.0}
