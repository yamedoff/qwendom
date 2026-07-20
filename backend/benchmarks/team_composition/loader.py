"""Sealed fixture loader for the Layer A team-composition benchmark."""

from __future__ import annotations

from benchmarks.team_composition.models import PrivateScenarioLabel, PublicScenario
from benchmarks.team_composition.private_labels import PRIVATE_LABEL_SUITE_HASH, load_private_label, load_private_labels
from benchmarks.team_composition.public_suite import PUBLIC_SUITE_HASH, load_public_scenario, load_public_scenarios


def load_public_suite() -> tuple[list[PublicScenario], str]:
    """Return the frozen public suite and its stable suite hash."""

    return load_public_scenarios(), PUBLIC_SUITE_HASH


def load_private_suite() -> tuple[dict[str, PrivateScenarioLabel], str]:
    """Return the frozen private labels and their stable suite hash."""

    return load_private_labels(), PRIVATE_LABEL_SUITE_HASH


def load_scenario_pair(scenario_id: str) -> tuple[PublicScenario, PrivateScenarioLabel]:
    """Return one public/private scenario pair by ID."""

    return load_public_scenario(scenario_id), load_private_label(scenario_id)
