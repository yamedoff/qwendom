"""Private labels for the sealed Layer A team-composition benchmark."""

from __future__ import annotations

from benchmarks.team_composition.models import (
    DependencyEdgeLabel,
    PrivateScenarioLabel,
    RecoveryExpectation,
    TeamSizeExpectation,
    ValidatorRequirement,
    stable_hash,
)


PRIVATE_LABELS: tuple[PrivateScenarioLabel, ...] = (
    PrivateScenarioLabel(
        scenario_id="evidence-brief",
        required_capabilities=["evidence_gathering", "validation"],
        optional_capabilities=["citation_retrieval"],
        forbidden_capabilities=["sandbox_execution", "image_generation", "video_generation", "data_analysis"],
        forbidden_tool_grants=["execute_command", "run_code", "start_execution_environment", "generate_images", "submit_text_to_video"],
        required_validator_requirements=[
            ValidatorRequirement(
                target_template_ids=["researcher"],
                allowed_validator_template_ids=["critic", "architect"],
                required_checks=["evidence_citations"],
            )
        ],
        required_dependency_edges=[DependencyEdgeLabel(producer_template_id="researcher", consumer_template_id="critic")],
        allowed_dependency_edges=[DependencyEdgeLabel(producer_template_id="researcher", consumer_template_id="critic")],
        team_size_expectation=TeamSizeExpectation(min_assignments=2, max_assignments=2, max_projected_cost_units=3),
    ),
    PrivateScenarioLabel(
        scenario_id="repository-repair",
        required_capabilities=["implementation", "sandbox_execution", "test_execution"],
        optional_capabilities=["architecture"],
        forbidden_capabilities=["image_generation", "video_generation"],
        forbidden_tool_grants=["generate_images", "submit_text_to_video"],
        required_validator_requirements=[
            ValidatorRequirement(
                target_template_ids=["builder"],
                allowed_validator_template_ids=["test_engineer", "critic"],
                required_checks=["executed_checks"],
            )
        ],
        required_dependency_edges=[DependencyEdgeLabel(producer_template_id="builder", consumer_template_id="test_engineer")],
        allowed_dependency_edges=[
            DependencyEdgeLabel(producer_template_id="architect", consumer_template_id="builder"),
            DependencyEdgeLabel(producer_template_id="builder", consumer_template_id="test_engineer"),
            DependencyEdgeLabel(producer_template_id="builder", consumer_template_id="critic"),
        ],
        forbidden_parallel_template_pairs=[("builder", "builder")],
        team_size_expectation=TeamSizeExpectation(min_assignments=2, max_assignments=3, max_projected_cost_units=6),
    ),
    PrivateScenarioLabel(
        scenario_id="screenshot-to-product",
        required_capabilities=["ui_engineering", "sandbox_execution", "validation"],
        optional_capabilities=["artifact_export"],
        forbidden_capabilities=["video_generation", "data_analysis"],
        forbidden_tool_grants=["submit_text_to_video", "run_code"],
        required_validator_requirements=[
            ValidatorRequirement(
                target_template_ids=["frontend_engineer"],
                allowed_validator_template_ids=["test_engineer", "critic"],
                required_checks=["functional_validation"],
            )
        ],
        required_dependency_edges=[DependencyEdgeLabel(producer_template_id="frontend_engineer", consumer_template_id="test_engineer")],
        allowed_dependency_edges=[
            DependencyEdgeLabel(producer_template_id="frontend_engineer", consumer_template_id="test_engineer"),
            DependencyEdgeLabel(producer_template_id="frontend_engineer", consumer_template_id="critic"),
        ],
        team_size_expectation=TeamSizeExpectation(min_assignments=2, max_assignments=3, max_projected_cost_units=7),
    ),
    PrivateScenarioLabel(
        scenario_id="cross-media-launch",
        required_capabilities=["evidence_gathering", "image_generation", "video_generation", "validation"],
        optional_capabilities=["artifact_publishing"],
        forbidden_capabilities=["data_analysis"],
        forbidden_tool_grants=["run_code"],
        required_validator_requirements=[
            ValidatorRequirement(
                target_template_ids=["image_creator", "video_producer"],
                allowed_validator_template_ids=["critic"],
                required_checks=["artifact_provenance"],
            )
        ],
        required_dependency_edges=[
            DependencyEdgeLabel(producer_template_id="researcher", consumer_template_id="critic"),
            DependencyEdgeLabel(producer_template_id="image_creator", consumer_template_id="critic"),
            DependencyEdgeLabel(producer_template_id="video_producer", consumer_template_id="critic"),
        ],
        allowed_dependency_edges=[
            DependencyEdgeLabel(producer_template_id="researcher", consumer_template_id="critic"),
            DependencyEdgeLabel(producer_template_id="image_creator", consumer_template_id="critic"),
            DependencyEdgeLabel(producer_template_id="video_producer", consumer_template_id="critic"),
        ],
        team_size_expectation=TeamSizeExpectation(min_assignments=4, max_assignments=4, max_projected_cost_units=9),
    ),
    PrivateScenarioLabel(
        scenario_id="data-investigation",
        required_capabilities=["data_analysis", "evidence_gathering", "validation"],
        optional_capabilities=["sandbox_execution"],
        forbidden_capabilities=["image_generation", "video_generation", "ui_engineering"],
        forbidden_tool_grants=["generate_images", "submit_text_to_video", "browser_test"],
        required_validator_requirements=[
            ValidatorRequirement(
                target_template_ids=["data_analyst"],
                allowed_validator_template_ids=["critic", "test_engineer"],
                required_checks=["independent_validation"],
            )
        ],
        required_dependency_edges=[
            DependencyEdgeLabel(producer_template_id="data_analyst", consumer_template_id="critic"),
            DependencyEdgeLabel(producer_template_id="researcher", consumer_template_id="critic"),
        ],
        allowed_dependency_edges=[
            DependencyEdgeLabel(producer_template_id="data_analyst", consumer_template_id="critic"),
            DependencyEdgeLabel(producer_template_id="researcher", consumer_template_id="critic"),
        ],
        team_size_expectation=TeamSizeExpectation(min_assignments=3, max_assignments=3, max_projected_cost_units=6),
        recovery_expectation=RecoveryExpectation(
            unavailable_template_id="data_analyst",
            recoverable=False,
            expected_blocker_category="missing_system_capability",
        ),
    ),
    PrivateScenarioLabel(
        scenario_id="ambiguous-high-impact",
        expected_blocker_category="missing_user_input",
        team_size_expectation=TeamSizeExpectation(min_assignments=0, max_assignments=1, max_projected_cost_units=1),
    ),
    PrivateScenarioLabel(
        scenario_id="small-bounded-task",
        required_capabilities=["evidence_gathering"],
        optional_capabilities=["citation_retrieval"],
        forbidden_capabilities=["sandbox_execution", "image_generation", "video_generation", "data_analysis", "ui_engineering"],
        forbidden_tool_grants=["execute_command", "generate_images", "submit_text_to_video"],
        team_size_expectation=TeamSizeExpectation(min_assignments=1, max_assignments=1, max_projected_cost_units=2),
    ),
)

PRIVATE_LABEL_INDEX = {label.scenario_id: label for label in PRIVATE_LABELS}
PRIVATE_LABEL_SUITE_HASH = stable_hash([label.model_dump(mode="json") for label in PRIVATE_LABELS])


def load_private_labels() -> dict[str, PrivateScenarioLabel]:
    """Return deep-copied private labels keyed by scenario ID."""

    return {
        label.scenario_id: PrivateScenarioLabel.model_validate(label.model_dump(mode="json"))
        for label in PRIVATE_LABELS
    }


def load_private_label(scenario_id: str) -> PrivateScenarioLabel:
    """Return one private label set by scenario ID."""

    return PrivateScenarioLabel.model_validate(PRIVATE_LABEL_INDEX[scenario_id].model_dump(mode="json"))
