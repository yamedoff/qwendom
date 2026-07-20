"""Focused tests for bounded Phase 1 team-composition validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.capability_registry import canonical_tool_id, get_role_capabilities
from society.schemas.team_composition import (
    CompositionLimits,
    TeamAssignment,
    TeamCompositionPlan,
    TeamCompositionValidationError,
    ToolGrant,
    WorkNode,
    validate_team_composition_plan,
)


REGISTRY = {
    role: get_role_capabilities(role)
    for role in [
        "coordinator",
        "architect",
        "researcher",
        "builder",
        "frontend_engineer",
        "test_engineer",
        "image_creator",
        "video_producer",
        "data_analyst",
        "critic",
    ]
}


def _issues(error: TeamCompositionValidationError) -> set[str]:
    return {issue.code for issue in error.issues}


def _builder_assignment() -> TeamAssignment:
    return TeamAssignment(
        id="builder-1",
        agent_template_id="builder",
        objective="Implement the backend change",
        required_capabilities=["implementation", "sandbox_execution"],
        tool_grants=[
            ToolGrant(
                capability="implementation",
                tool_ids=[
                    "repository_write",
                    "start_execution_environment",
                    "execute_command",
                    "export_artifact",
                    "close_execution_environment",
                ],
            )
        ],
        owned_paths=["backend/service"],
        expected_artifacts=["patch"],
    )


def _critic_assignment() -> TeamAssignment:
    return TeamAssignment(
        id="critic-1",
        agent_template_id="critic",
        objective="Independently validate the implementation",
        required_capabilities=["validation"],
        tool_grants=[
            ToolGrant(capability="validation", tool_ids=["read_artifacts", "rubric_check"])
        ],
        acceptance_checks=["artifact_diff_review", "regression_checks"],
        validates_assignment_ids=["builder-1"],
    )


def _valid_plan() -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Implement a bounded backend change with independent validation.",
        assignments=[
            TeamAssignment(
                id="coord-1",
                agent_template_id="coordinator",
                objective="Select the team and define the graph",
                required_capabilities=["team_planning"],
                tool_grants=[ToolGrant(capability="team_planning", tool_ids=["team_plan"])],
            ),
            _builder_assignment(),
            _critic_assignment(),
        ],
        work_graph=[
            WorkNode(id="node-coord", assignment_id="coord-1"),
            WorkNode(id="node-build", assignment_id="builder-1", depends_on=["node-coord"], estimated_cost_class="medium"),
            WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-build"]),
        ],
        selection_rationale="The task needs one implementation owner and one independent validator.",
    )


class TeamCompositionValidationTests(unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        plan = _valid_plan()
        validate_team_composition_plan(
            plan,
            REGISTRY,
            required_capabilities={"team_planning", "implementation", "validation"},
            available_tool_ids={
                "team_plan",
                "repository_write",
                "start_execution_environment",
                "execute_command",
                "export_artifact",
                "close_execution_environment",
                "read_artifacts",
                "rubric_check",
            },
            limits=CompositionLimits(max_total_cost_units=4),
        )

    def test_mutable_defaults_are_isolated(self) -> None:
        first = TeamAssignment(id="a", agent_template_id="coordinator", objective="one")
        second = TeamAssignment(id="b", agent_template_id="coordinator", objective="two")
        first.required_capabilities.append("team_planning")
        first.tool_grants.append(ToolGrant(capability="team_planning", tool_ids=["team_plan"]))
        self.assertEqual(second.required_capabilities, [])
        self.assertEqual(second.tool_grants, [])

    def test_forbidden_and_unavailable_grants_fail(self) -> None:
        plan = _valid_plan()
        plan.assignments[1].tool_grants = [
            ToolGrant(
                capability="implementation",
                tool_ids=["repository_write", "execute_command", "forbidden_tool"],
            )
        ]
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(
                plan,
                REGISTRY,
                available_tool_ids={"team_plan", "repository_write", "read_artifacts", "rubric_check"},
            )
        self.assertIn("forbidden_tool_grant", _issues(exc.exception))
        self.assertIn("unavailable_tool_grant", _issues(exc.exception))

    def test_unspecified_runtime_availability_does_not_enforce_allowlists(self) -> None:
        plan = _valid_plan()
        validate_team_composition_plan(
            plan,
            REGISTRY,
            required_capabilities={"team_planning", "implementation", "validation"},
            available_tool_ids=None,
            available_agent_template_ids=None,
            limits=CompositionLimits(max_total_cost_units=4),
        )

    def test_explicit_empty_runtime_allowlists_reject_grants_and_templates(self) -> None:
        plan = _valid_plan()
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(
                plan,
                REGISTRY,
                available_tool_ids=[],
                available_agent_template_ids=[],
            )
        issue_codes = _issues(exc.exception)
        self.assertIn("unavailable_tool_grant", issue_codes)
        self.assertIn("unavailable_agent_template", issue_codes)

    def test_missing_independent_validation_fails(self) -> None:
        plan = _valid_plan()
        plan.assignments = plan.assignments[:2]
        plan.work_graph = plan.work_graph[:2]
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(plan, REGISTRY)
        self.assertIn("artifact_without_independent_validation", _issues(exc.exception))

    def test_self_validation_fails(self) -> None:
        plan = _valid_plan()
        plan.assignments[2].validates_assignment_ids = ["critic-1"]
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(plan, REGISTRY)
        self.assertIn("self_validation", _issues(exc.exception))

    def test_graph_cycles_and_missing_references_fail(self) -> None:
        plan = _valid_plan()
        plan.work_graph[1].depends_on = ["node-missing"]
        plan.work_graph[2].depends_on = ["node-critic"]
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(plan, REGISTRY)
        self.assertIn("unknown_dependency", _issues(exc.exception))
        self.assertIn("cyclic_work_graph", _issues(exc.exception))

    def test_independently_ready_path_and_domain_conflicts_fail(self) -> None:
        plan = TeamCompositionPlan(
            task_summary="Two builders cannot safely run together.",
            assignments=[
                TeamAssignment(
                    id="builder-a",
                    agent_template_id="builder",
                    objective="Edit shared file",
                    required_capabilities=["implementation", "sandbox_execution"],
                    tool_grants=[
                        ToolGrant(
                            capability="implementation",
                            tool_ids=[
                                "repository_write",
                                "start_execution_environment",
                                "execute_command",
                                "export_artifact",
                                "close_execution_environment",
                            ],
                        )
                    ],
                    owned_paths=["backend/shared"],
                    expected_artifacts=["patch-a"],
                ),
                TeamAssignment(
                    id="builder-b",
                    agent_template_id="builder",
                    objective="Edit overlapping file",
                    required_capabilities=["implementation", "sandbox_execution"],
                    tool_grants=[
                        ToolGrant(
                            capability="implementation",
                            tool_ids=[
                                "repository_write",
                                "start_execution_environment",
                                "execute_command",
                                "export_artifact",
                                "close_execution_environment",
                            ],
                        )
                    ],
                    owned_paths=["backend/shared/api.py"],
                    expected_artifacts=["patch-b"],
                ),
                TeamAssignment(
                    id="critic-1",
                    agent_template_id="critic",
                    objective="Validate both builders",
                    required_capabilities=["validation"],
                    tool_grants=[ToolGrant(capability="validation", tool_ids=["read_artifacts"])],
                    acceptance_checks=["artifact_diff_review"],
                    validates_assignment_ids=["builder-a", "builder-b"],
                ),
            ],
            work_graph=[
                WorkNode(id="node-a", assignment_id="builder-a", conflict_domains=["repo"]),
                WorkNode(id="node-b", assignment_id="builder-b", conflict_domains=["repo"]),
                WorkNode(id="node-c", assignment_id="critic-1", depends_on=["node-a", "node-b"]),
            ],
            selection_rationale="Concurrent conflicting builders must be rejected.",
        )
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(plan, REGISTRY)
        self.assertIn("concurrent_work_conflict", _issues(exc.exception))

    def test_ui_conflict_domains_are_normalized_during_static_validation(self) -> None:
        plan = TeamCompositionPlan(
            task_summary="Static validation should reject equivalent UI conflict labels.",
            assignments=[
                TeamAssignment(
                    id="builder-a",
                    agent_template_id="builder",
                    objective="Edit UI surface A",
                    required_capabilities=["implementation"],
                    tool_grants=[ToolGrant(capability="implementation", tool_ids=["repository_write"])],
                ),
                TeamAssignment(
                    id="builder-b",
                    agent_template_id="builder",
                    objective="Edit UI surface B",
                    required_capabilities=["implementation"],
                    tool_grants=[ToolGrant(capability="implementation", tool_ids=["repository_write"])],
                ),
            ],
            work_graph=[
                WorkNode(id="node-a", assignment_id="builder-a", conflict_domains=[" UI "]),
                WorkNode(id="node-b", assignment_id="builder-b", conflict_domains=["ui"]),
            ],
            selection_rationale="Equivalent conflict-domain labels must be treated identically.",
        )
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(plan, REGISTRY)
        self.assertIn("concurrent_work_conflict", _issues(exc.exception))

    def test_required_execution_and_media_tools_fail_when_omitted(self) -> None:
        builder_plan = _valid_plan()
        builder_plan.assignments[1].tool_grants = [
            ToolGrant(capability="implementation", tool_ids=["repository_write"])
        ]
        with self.assertRaises(TeamCompositionValidationError) as builder_exc:
            validate_team_composition_plan(builder_plan, REGISTRY)
        self.assertIn("missing_required_tool", _issues(builder_exc.exception))

        media_plan = TeamCompositionPlan(
            task_summary="Image tasks need collection and provenance checks.",
            assignments=[
                TeamAssignment(
                    id="image-1",
                    agent_template_id="image_creator",
                    objective="Generate a launch image",
                    required_capabilities=["image_generation"],
                    tool_grants=[
                        ToolGrant(capability="image_generation", tool_ids=["generate_image", "inspect_image"])
                    ],
                    expected_artifacts=["hero-image"],
                ),
                TeamAssignment(
                    id="critic-1",
                    agent_template_id="critic",
                    objective="Validate the image artifact",
                    required_capabilities=["validation"],
                    tool_grants=[ToolGrant(capability="validation", tool_ids=["read_artifacts"])],
                    acceptance_checks=["artifact_collection"],
                    validates_assignment_ids=["image-1"],
                ),
            ],
            work_graph=[
                WorkNode(id="node-image", assignment_id="image-1"),
                WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-image"]),
            ],
            selection_rationale="Media validation must prove collection and provenance.",
        )
        with self.assertRaises(TeamCompositionValidationError) as media_exc:
            validate_team_composition_plan(media_plan, REGISTRY)
        self.assertIn("missing_required_tool", _issues(media_exc.exception))
        self.assertIn("missing_required_validation_check", _issues(media_exc.exception))

    def test_limits_fail_before_any_external_callback_could_run(self) -> None:
        plan = TeamCompositionPlan(
            task_summary="Ready-layer limits are enforced before execution.",
            assignments=[
                TeamAssignment(
                    id="builder-1",
                    agent_template_id="builder",
                    objective="Change one module",
                    required_capabilities=["implementation", "sandbox_execution"],
                    tool_grants=[
                        ToolGrant(
                            capability="implementation",
                            tool_ids=[
                                "repository_write",
                                "start_execution_environment",
                                "execute_command",
                                "export_artifact",
                                "close_execution_environment",
                            ],
                        )
                    ],
                    expected_artifacts=["patch-1"],
                ),
                TeamAssignment(
                    id="builder-2",
                    agent_template_id="builder",
                    objective="Change another module",
                    required_capabilities=["implementation", "sandbox_execution"],
                    tool_grants=[
                        ToolGrant(
                            capability="implementation",
                            tool_ids=[
                                "repository_write",
                                "start_execution_environment",
                                "execute_command",
                                "export_artifact",
                                "close_execution_environment",
                            ],
                        )
                    ],
                    expected_artifacts=["patch-2"],
                ),
                TeamAssignment(
                    id="builder-3",
                    agent_template_id="builder",
                    objective="Change a third module",
                    required_capabilities=["implementation", "sandbox_execution"],
                    tool_grants=[
                        ToolGrant(
                            capability="implementation",
                            tool_ids=[
                                "repository_write",
                                "start_execution_environment",
                                "execute_command",
                                "export_artifact",
                                "close_execution_environment",
                            ],
                        )
                    ],
                    expected_artifacts=["patch-3"],
                ),
                TeamAssignment(
                    id="image-1",
                    agent_template_id="image_creator",
                    objective="Generate a hero asset",
                    required_capabilities=["image_generation"],
                    tool_grants=[
                        ToolGrant(
                            capability="image_generation",
                            tool_ids=["generate_image", "inspect_image", "publish_image"],
                        )
                    ],
                    expected_artifacts=["hero-image"],
                ),
                TeamAssignment(
                    id="critic-1",
                    agent_template_id="critic",
                    objective="Validate producer artifacts",
                    required_capabilities=["validation"],
                    tool_grants=[ToolGrant(capability="validation", tool_ids=["read_artifacts"])],
                    acceptance_checks=["artifact_collection", "artifact_provenance", "artifact_diff_review"],
                    validates_assignment_ids=["builder-1", "builder-2", "builder-3", "image-1"],
                ),
            ],
            work_graph=[
                WorkNode(id="node-b1", assignment_id="builder-1", estimated_cost_class="high"),
                WorkNode(id="node-b2", assignment_id="builder-2", estimated_cost_class="high"),
                WorkNode(id="node-b3", assignment_id="builder-3", estimated_cost_class="high"),
                WorkNode(id="node-image", assignment_id="image-1", estimated_cost_class="medium"),
                WorkNode(
                    id="node-critic",
                    assignment_id="critic-1",
                    depends_on=["node-b1", "node-b2", "node-b3", "node-image"],
                ),
            ],
            selection_rationale="This plan intentionally exceeds bounded concurrency and cost caps.",
        )
        with self.assertRaises(TeamCompositionValidationError) as exc:
            validate_team_composition_plan(
                plan,
                REGISTRY,
                limits=CompositionLimits(
                    max_model_workers=2,
                    max_agentbay_sessions=1,
                    max_media_jobs=0,
                    max_dynamic_specialists=3,
                    max_total_cost_units=5,
                ),
            )
        issue_codes = _issues(exc.exception)
        self.assertIn("model_worker_limit_exceeded", issue_codes)
        self.assertIn("agentbay_session_limit_exceeded", issue_codes)
        self.assertIn("media_job_limit_exceeded", issue_codes)
        self.assertIn("dynamic_specialist_limit_exceeded", issue_codes)
        self.assertIn("cost_limit_exceeded", issue_codes)

    def test_legacy_alias_grants_pass_when_canonical_tools_are_available(self) -> None:
        plan = _valid_plan()
        plan.assignments[1].tool_grants = [
            ToolGrant(
                capability="implementation",
                tool_ids=[
                    "repository_write",
                    "start_execution_environment",
                    "agentbay_execute",
                    "agentbay_export_artifact",
                    "close_execution_environment",
                ],
            )
        ]
        validate_team_composition_plan(
            plan,
            REGISTRY,
            available_tool_ids={
                "team_plan",
                "repository_write",
                "start_execution_environment",
                "execute_command",
                "export_artifact",
                "close_execution_environment",
                "read_artifacts",
                "rubric_check",
            },
        )

    def test_legacy_agentbay_aliases_canonicalize_correctly(self) -> None:
        self.assertEqual(canonical_tool_id("agentbay_execute"), "execute_command")
        self.assertEqual(canonical_tool_id("agentbay_run_code"), "run_code")
        self.assertEqual(canonical_tool_id("agentbay_write_file"), "write_text_file")
        self.assertEqual(canonical_tool_id("agentbay_list_files"), "list_files")
        self.assertEqual(canonical_tool_id("agentbay_export_artifact"), "export_artifact")


if __name__ == "__main__":
    unittest.main()
