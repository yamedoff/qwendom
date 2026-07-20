"""Layer A team-composition benchmark package."""

from benchmarks.team_composition.loader import load_private_suite, load_public_suite, load_scenario_pair
from benchmarks.team_composition.models import (
    EVALUATOR_VERSION,
    FROZEN_OFFICIAL_CONFIG,
    SUITE_VERSION,
    EvaluatedTrialRecord,
    PrivateScenarioLabel,
    PublicScenario,
)

__all__ = [
    "EVALUATOR_VERSION",
    "EvaluatedTrialRecord",
    "FROZEN_OFFICIAL_CONFIG",
    "PrivateScenarioLabel",
    "PublicScenario",
    "SUITE_VERSION",
    "load_private_suite",
    "load_public_suite",
    "load_scenario_pair",
]
