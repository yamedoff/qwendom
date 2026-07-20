"""Public contract for benchmark v3's tool-discovered security audit."""

from __future__ import annotations

from pydantic import BaseModel, Field

class SecurityAuditTask(BaseModel):
    """Public task metadata; evidence records remain behind shared tools."""

    suite_version: str = "v3"
    task_id: str = "security-release-audit"
    kind: str = "security_release_audit"
    ask: str
    candidates: dict[str, str]
    stages: dict[str, str]
    constraints: dict[str, str]
    required_claim_labels: list[str]
    claim_requirements: dict[str, str]
    required_numeric_labels: list[str]
    numeric_requirements: dict[str, str]
    available_tools: list[str]


TASK = SecurityAuditTask(
    ask=(
        "Audit the release using the read-only security tools. Select every release blocker, "
        "order the required release stages, cite exact evidence for every claim label, and "
        "return the required numeric answers."
    ),
    candidates={
        "V01": "Missing object-level authorization on the admin export endpoint.",
        "V02": "Non-constant-time webhook signature comparison.",
        "V03": "Webhook replay window is not enforced.",
        "V04": "Webhook acknowledgement occurs before durable commit.",
        "V05": "Reachable critical dependency vulnerability.",
        "V06": "A live production secret is tracked in source control.",
        "V07": "Adversarial release tests are missing.",
        "N01": "Secure cookie attributes are already configured.",
        "N02": "The container base image is pinned by digest.",
    },
    stages={
        "G01": "Contain and rotate exposed credentials.",
        "G02": "Remediate code and dependency blockers in parallel.",
        "G03": "Run the complete adversarial security test gate.",
        "G04": "Deploy the canary and retain the verified rollback path.",
    },
    constraints={
        "C01": "No exposed production credential may remain valid.",
        "C02": "Every release blocker must have a passing adversarial test.",
        "C03": "Rollback must complete within 10 minutes.",
    },
    required_claim_labels=[
        "authz_blocker", "webhook_blockers", "supply_chain_blockers",
        "test_gap", "release_order", "rollback_constraint", "parallel_timing",
    ],
    claim_requirements={
        "authz_blocker": "Cite the smallest sufficient set proving the authorization blocker.",
        "webhook_blockers": "Cite the smallest sufficient set proving every webhook blocker.",
        "supply_chain_blockers": "Cite the smallest sufficient set proving every dependency or secret blocker.",
        "test_gap": "Cite the smallest sufficient set proving the adversarial-test gap.",
        "release_order": (
            "Prove the declared four-stage order plus the remediation contents, adversarial-test gate, "
            "and deployment gate. Cite the smallest set that proves all four parts."
        ),
        "rollback_constraint": (
            "Prove both the measured rollback duration and that the deployment gate retains the "
            "verified rollback path."
        ),
        "parallel_timing": (
            "Prove both the prerequisite containment duration and the complete parallel critical-path timing."
        ),
    },
    required_numeric_labels=["release_blockers", "critical_findings", "minimum_minutes"],
    numeric_requirements={
        "release_blockers": "Count every selected release blocker.",
        "critical_findings": (
            "Count selected blockers that are active authorization, webhook, dependency, or credential "
            "defects; do not count missing test coverage as a critical finding."
        ),
        "minimum_minutes": "Return the complete minimum critical-path duration through deployment.",
    },
    available_tools=[
        "inspect_auth_surface", "inspect_webhook_surface",
        "inspect_supply_chain_surface", "inspect_release_pipeline_surface",
        "lookup_security_record", "calculate",
    ],
)


def required_output_example() -> dict[str, object]:
    """Return a type-only example that exposes no candidate or evidence answer."""

    return {
        "selected_ids": ["candidate ID"],
        "ordered_ids": ["stage ID"],
        "evidence_citations": {"required claim label": ["record ID"]},
        "constraint_ids": ["constraint ID"],
        "numeric_answers": {"required numeric label": 0},
        "governance_activity": ["brief optional activity label"],
    }
