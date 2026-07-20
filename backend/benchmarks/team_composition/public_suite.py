"""Frozen public scenarios for the Layer A team-composition benchmark."""

from __future__ import annotations

from benchmarks.team_composition.models import PublicFixture, PublicScenario, stable_hash
from society.capability_registry import canonical_tool_id, get_role_capabilities
from society.schemas.team_composition import CompositionLimits


def _stable_distinct(values: list[str]) -> list[str]:
    """Return deterministic unique string values while preserving first order."""

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _available_tool_ids(agent_template_ids: list[str]) -> list[str]:
    """Return the immutable tool snapshot used by the sealed Layer A suite.

    ``browser_render`` was added after the 2026-07-15 official collection. It
    must not retroactively change that suite's public hash or fairness surface.
    """

    tool_ids: list[str] = []
    for agent_template_id in agent_template_ids:
        registration = get_role_capabilities(agent_template_id)
        if registration is None:
            raise ValueError(f"unknown public agent template {agent_template_id!r}")
        for tool_id in registration.allowed_tools:
            if tool_id == "browser_render":
                continue
            # Preserve the pre-browser-runtime public spelling captured by the
            # sealed suite; the mutable runtime alias now maps this identifier.
            tool_ids.append("browser_test" if tool_id == "browser_test" else canonical_tool_id(tool_id))
    return sorted(_stable_distinct(tool_ids))


def _scenario_availability(agent_template_ids: list[str]) -> dict[str, list[str]]:
    """Build deterministic public availability metadata for one scenario."""

    template_ids = _stable_distinct(agent_template_ids)
    return {
        "available_agent_template_ids": template_ids,
        "available_tool_ids": _available_tool_ids(template_ids),
    }


PUBLIC_SCENARIOS: tuple[PublicScenario, ...] = (
    PublicScenario(
        scenario_id="evidence-brief",
        family="evidence_brief",
        title="Evidence Brief With Independent Review",
        request=(
            "Prepare a cited evidence brief explaining whether the current provider-region policy "
            "supports the requested rollout. Do not modify code or generate media."
        ),
        fixtures=[
            PublicFixture(
                fixture_id="provider-matrix",
                description="Current rollout constraints extracted from internal planning notes.",
                content="Provider rollout requires a written evidence brief with cited sources and a final acceptance review.",
            ),
            PublicFixture(
                fixture_id="artifact-contract",
                description="Deliverable contract.",
                content="Return a prose brief and a short independent review artifact. Product file writes are out of scope.",
            ),
        ],
        acceptance_requirements=["evidence_citations", "independent_review"],
        limits=CompositionLimits(max_dynamic_specialists=3, max_total_cost_units=4),
        **_scenario_availability(["coordinator", "researcher", "critic", "architect"]),
    ),
    PublicScenario(
        scenario_id="repository-repair",
        family="repository_repair",
        title="Repository Repair With Execution Validation",
        request="Repair a failing backend fixture repository, run the targeted checks, and provide an independent validation report.",
        fixtures=[
            PublicFixture(
                fixture_id="repo-fixture",
                description="A bounded backend fixture with one failing behavior and existing targeted tests.",
                content="The repository contains a backend bug in a small module. Targeted tests exist and should be executed after the fix.",
            ),
        ],
        acceptance_requirements=["executed_checks", "independent_validation", "artifact_export"],
        limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=6),
        **_scenario_availability(["coordinator", "builder", "test_engineer", "critic", "architect"]),
    ),
    PublicScenario(
        scenario_id="screenshot-to-product",
        family="screenshot_to_product",
        title="Screenshot To Working Product",
        request="Implement the referenced interface from screenshots, verify responsive behavior, and produce an independent functional review.",
        fixtures=[
            PublicFixture(
                fixture_id="desktop-shot",
                description="Desktop reference screenshot summary.",
                content="A marketing page requires layout fidelity, interactive navigation, and a responsive mobile fallback.",
            ),
            PublicFixture(
                fixture_id="mobile-shot",
                description="Mobile reference screenshot summary.",
                content="The mobile view must preserve hierarchy and navigation while remaining interactive.",
            ),
        ],
        acceptance_requirements=["responsive_validation", "functional_validation"],
        limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=7),
        **_scenario_availability(["coordinator", "frontend_engineer", "test_engineer", "critic"]),
    ),
    PublicScenario(
        scenario_id="cross-media-launch",
        family="cross_media_launch",
        title="Cross-Media Launch Campaign",
        request="Prepare a coordinated launch package including research-backed copy, a social image, and a short video concept with independent critique.",
        fixtures=[
            PublicFixture(
                fixture_id="campaign-brief",
                description="Public campaign brief.",
                content="The package needs a factual message, one image artifact, one short video artifact, and an acceptance review.",
            ),
        ],
        acceptance_requirements=["artifact_provenance", "independent_review"],
        limits=CompositionLimits(max_dynamic_specialists=5, max_total_cost_units=10),
        **_scenario_availability(["coordinator", "researcher", "image_creator", "video_producer", "critic"]),
    ),
    PublicScenario(
        scenario_id="data-investigation",
        family="data_investigation",
        title="Data Investigation With Domain Research",
        request="Investigate the provided dataset anomaly, produce evidence-backed findings, and include an independent validation summary.",
        fixtures=[
            PublicFixture(
                fixture_id="dataset-summary",
                description="Structured anomaly summary.",
                content="The dataset includes a retention anomaly that needs analysis, supporting research, and a validation report.",
            ),
        ],
        acceptance_requirements=["analysis_artifact", "evidence_citations", "independent_validation"],
        limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=7),
        **_scenario_availability([
            "coordinator",
            "architect",
            "researcher",
            "builder",
            "critic",
            "test_engineer",
            "image_creator",
            "video_producer",
        ]),
        injected_unavailable_template_id="data_analyst",
    ),
    PublicScenario(
        scenario_id="ambiguous-high-impact",
        family="ambiguous_high_impact",
        title="Ambiguous High-Impact Task",
        request="Proceed with a high-impact production change affecting customer data, but the request omits approval scope, acceptance criteria, and rollback policy.",
        fixtures=[
            PublicFixture(
                fixture_id="missing-approvals",
                description="Known ambiguity summary.",
                content="The requester did not specify exact approval scope, rollback requirements, or safe operating boundaries.",
            ),
        ],
        unresolved_user_requirements=[
            "Clarify the approval scope.",
            "Clarify the acceptance and rollback requirements.",
        ],
        limits=CompositionLimits(max_dynamic_specialists=2, max_total_cost_units=3),
        **_scenario_availability(["coordinator", "architect", "researcher", "builder", "critic"]),
    ),
    PublicScenario(
        scenario_id="small-bounded-task",
        family="small_bounded_task",
        title="Small Bounded Task",
        request="Answer one narrow evidence question using the provided notes. No code changes, media work, or independent acceptance artifact is required.",
        fixtures=[
            PublicFixture(
                fixture_id="single-note",
                description="One bounded note.",
                content="The task is a small factual clarification that fits in one specialist pass.",
            ),
        ],
        acceptance_requirements=["bounded_answer"],
        limits=CompositionLimits(max_dynamic_specialists=1, max_total_cost_units=2),
        **_scenario_availability(["coordinator", "researcher"]),
    ),
)

PUBLIC_SCENARIO_INDEX = {scenario.scenario_id: scenario for scenario in PUBLIC_SCENARIOS}
PUBLIC_SUITE_HASH = stable_hash([scenario.model_dump(mode="json") for scenario in PUBLIC_SCENARIOS])


def load_public_scenarios() -> list[PublicScenario]:
    """Return deep-validated frozen public scenarios in deterministic order."""

    return [PublicScenario.model_validate(scenario.model_dump(mode="json")) for scenario in PUBLIC_SCENARIOS]


def load_public_scenario(scenario_id: str) -> PublicScenario:
    """Return one frozen public scenario by ID."""

    scenario = PUBLIC_SCENARIO_INDEX[scenario_id]
    return PublicScenario.model_validate(scenario.model_dump(mode="json"))
