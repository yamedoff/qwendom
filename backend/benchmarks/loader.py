"""Loader and prompt builder for the benchmark reference task.

The prompt builder constructs the model-facing prompt from the public
incident packet only.  Ground truth is never imported or referenced here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.fixtures.incident_packet import (
    CANDIDATE_ACTION_IDS,
    CANDIDATE_CONTROL_IDS,
    CANDIDATE_HYPOTHESIS_IDS,
    FACTS,
    STAKEHOLDER_CONSTRAINTS,
    STAKEHOLDER_CONSTRAINT_IDS,
    get_incident_packet,
)


def load_incident_packet() -> dict[str, Any]:
    """Load the public incident packet fixture."""
    return get_incident_packet()


def build_prompt(mode: str = "single_agent") -> str:
    """Build the model-facing prompt from the public incident packet.

    The ground-truth fixture is never included.  The prompt instructs the
    model to return a strict JSON object matching the IncidentDecision schema.

    Args:
        mode: Accepted for caller compatibility but deliberately omitted from
            the prompt so both modes receive byte-identical task input.

    Returns:
        The complete prompt string.
    """
    facts_block = "\n".join(f"  {fid}: {text}" for fid, text in FACTS.items())
    hypotheses_block = "\n".join(
        f"  {hid}: {text}" for hid, text in _hypotheses_text().items()
    )
    actions_block = "\n".join(
        f"  {aid}: {text}" for aid, text in _actions_text().items()
    )
    controls_block = "\n".join(
        f"  {cid}: {text}" for cid, text in _controls_text().items()
    )
    constraints_block = "\n".join(
        f"  {scid}: {text}"
        for scid, text in STAKEHOLDER_CONSTRAINTS.items()
    )

    prompt = f"""\
You are analysing a production incident.

## Facts
{facts_block}

## Candidate Hypotheses
{hypotheses_block}

## Candidate Actions
{actions_block}

## Candidate Controls
{controls_block}

## Stakeholder Constraints
{constraints_block}

## Instructions
Select exactly one root-cause hypothesis, recommend actions (ordered), \
recommend preventive controls, and cite evidence using fact IDs only.

Return a single JSON object with this schema:
{{
  "root_cause_hypothesis_id": "<one of {CANDIDATE_HYPOTHESIS_IDS}>",
  "hypothesis_evidence": ["<fact IDs>"],
  "recommended_actions": ["<ordered action IDs from {CANDIDATE_ACTION_IDS}>"],
  "recommended_controls": ["<control IDs from {CANDIDATE_CONTROL_IDS}>"],
  "evidence_citations": {{"<claim>": ["<fact IDs>"]}},
  "stakeholder_constraints_acknowledged": ["<constraint IDs from {STAKEHOLDER_CONSTRAINT_IDS}>"],
  "governance_activity": ["<any governance steps taken>"]
}}
"""
    return prompt


def _hypotheses_text() -> dict[str, str]:
    from benchmarks.fixtures.incident_packet import HYPOTHESES
    return HYPOTHESES


def _actions_text() -> dict[str, str]:
    from benchmarks.fixtures.incident_packet import ACTIONS
    return ACTIONS


def _controls_text() -> dict[str, str]:
    from benchmarks.fixtures.incident_packet import CONTROLS
    return CONTROLS
