"""Public incident packet — the only fixture visible to the model prompt.

This module defines the reference incident used by both single-agent and
society benchmark modes.  It contains:

* 25 facts (F01–F25), including 3 contradiction pairs and 2 red herrings.
* 5 candidate hypotheses (H01–H05).
* 5 candidate actions (A01–A05).
* 5 candidate controls (C01–C05).
* 3 stakeholder constraints (SC01–SC03).

The ground truth (correct answers) lives in a **separate** module and is
never imported by the prompt builder.
"""

from __future__ import annotations

from typing import Any

FACT_IDS: list[str] = [f"F{i:02d}" for i in range(1, 26)]

FACTS: dict[str, str] = {
    "F01": "At 02:14 UTC, monitoring detected elevated error rates on api-gateway.",
    "F02": "Error rate on api-gateway spiked from 0.1% to 12.3% over 90 seconds.",
    "F03": "Database connection pool reached 95% utilization at 02:10 UTC.",
    "F04": "A deployment of payment-service v2.4.1 completed at 02:05 UTC.",
    "F05": "payment-service v2.4.1 introduced a new connection pooling library.",
    "F06": "The new pooling library has a known excessive-allocation behaviour in versions prior to 2.4.2.",
    "F07": "Memory usage on payment-service pods increased by 340% after deployment.",
    "F08": "api-gateway depends on payment-service for transaction validation.",
    "F09": "Load balancer health checks for payment-service began failing at 02:12 UTC.",
    "F10": "Auto-scaling triggered for payment-service at 02:11 UTC.",
    "F11": "New payment-service pods failed to start due to OOM killer.",
    "F12": "CPU utilization on existing payment-service pods remained below 40% throughout the incident.",
    "F13": "Network latency between api-gateway and payment-service was under 5ms.",
    "F14": "SSL certificate for payment-service expires in 45 days.",
    "F15": "A scheduled database maintenance window was planned for 06:00 UTC.",
    "F16": "Database connection pool hard limit is 500 connections.",
    "F17": "payment-service v2.4.1 opens 50 connections per pod instead of the previous 10.",
    "F18": "There were 8 payment-service pods running before auto-scaling.",
    "F19": "Total database connections from payment-service reached 475 at 02:10 UTC.",
    "F20": "api-gateway request timeout is configured at 30 seconds.",
    "F21": "payment-service response time increased from 50ms to 28000ms at 02:13 UTC.",
    "F22": "The on-call engineer received a PagerDuty alert at 02:14 UTC.",
    "F23": "payment-service v2.4.0 used the previous pooling library with stable connection counts.",
    "F24": "The deployment was approved by the tech lead at 01:30 UTC.",
    "F25": "A rollback to payment-service v2.4.0 was initiated at 02:20 UTC.",
}

CONTRADICTIONS: list[dict[str, str]] = [
    {
        "id": "CONTRA_01",
        "fact_a": "F03",
        "fact_b": "F12",
        "tension": "High DB pool utilization (95%) alongside low CPU usage (<40%) appears inconsistent.",
    },
    {
        "id": "CONTRA_02",
        "fact_a": "F10",
        "fact_b": "F11",
        "tension": "Auto-scaling triggered but new pods failed to start — scaling up yet capacity did not increase.",
    },
    {
        "id": "CONTRA_03",
        "fact_a": "F04",
        "fact_b": "F23",
        "tension": "Deployment completed successfully yet the previous version had stable behaviour — change appears safe on the surface.",
    },
]

RED_HERRINGS: list[dict[str, str]] = [
    {
        "id": "RH_01",
        "fact_id": "F14",
        "reason": "SSL certificate expiry in 45 days is unrelated to the current connection-pool incident.",
    },
    {
        "id": "RH_02",
        "fact_id": "F15",
        "reason": "Scheduled database maintenance at 06:00 UTC is four hours after the incident and causally irrelevant.",
    },
]

HYPOTHESES: dict[str, str] = {
    "H01": "api-gateway misconfiguration caused the error spike.",
    "H02": "payment-service v2.4.1 introduced excessive per-pod connection allocation, exhausting the database pool and causing cascading timeouts in api-gateway.",
    "H03": "A network partition between api-gateway and payment-service caused health-check failures.",
    "H04": "The database itself reached its connection hard limit due to organic traffic growth.",
    "H05": "The SSL certificate nearing expiry triggered TLS renegotiation storms.",
}

ACTIONS: dict[str, str] = {
    "A01": "Rollback payment-service to v2.4.0.",
    "A02": "Increase the database connection pool hard limit to 1000.",
    "A03": "Reduce per-pod connection allocation to 10 and redeploy.",
    "A04": "Restart all api-gateway pods.",
    "A05": "Renew the SSL certificate for payment-service.",
}

CONTROLS: dict[str, str] = {
    "C01": "Add a CPU utilization alert at 80% for payment-service.",
    "C02": "Add a database connection pool utilization alert at 80%.",
    "C03": "Require a peer review for all deployment approvals.",
    "C04": "Add a deployment canary step that validates connection count per pod before full rollout.",
    "C05": "Migrate payment-service to a serverless runtime.",
}

STAKEHOLDER_CONSTRAINTS: dict[str, str] = {
    "SC01": "Rollback must complete within 10 minutes.",
    "SC02": "No data loss is acceptable during remediation.",
    "SC03": "The fix must not require a database schema change.",
}

CANDIDATE_HYPOTHESIS_IDS: list[str] = list(HYPOTHESES.keys())
CANDIDATE_ACTION_IDS: list[str] = list(ACTIONS.keys())
CANDIDATE_CONTROL_IDS: list[str] = list(CONTROLS.keys())
STAKEHOLDER_CONSTRAINT_IDS: list[str] = list(STAKEHOLDER_CONSTRAINTS.keys())


def get_incident_packet() -> dict[str, Any]:
    """Return the full public incident packet as a JSON-serialisable dict."""
    return {
        "facts": FACTS,
        "contradictions": CONTRADICTIONS,
        "red_herrings": RED_HERRINGS,
        "candidate_hypotheses": HYPOTHESES,
        "candidate_actions": ACTIONS,
        "candidate_controls": CONTROLS,
        "stakeholder_constraints": STAKEHOLDER_CONSTRAINTS,
    }
