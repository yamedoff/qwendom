"""Deterministic tests for the Layer A team-composition benchmark harness."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.team_composition import cli as benchmark_cli
from benchmarks.team_composition.evaluator import evaluate_blocked, evaluate_plan
from benchmarks.team_composition.loader import load_private_suite, load_public_suite, load_scenario_pair
from benchmarks.team_composition.models import EVALUATOR_VERSION, FROZEN_OFFICIAL_CONFIG, SUITE_VERSION
from benchmarks.team_composition.persistence import load_evaluated_trials, persist_evaluated_trial
from benchmarks.team_composition.reporting import aggregate_trials, validate_official_freeze
from benchmarks.team_composition.runner import run_trial_sync
from society.schemas.team_composition import CompositionLimits, PlanValidationIssue, TeamAssignment, TeamCompositionPlan, ToolGrant, WorkNode
from society.team_composer import CompositionContext, TeamCompositionBlocked, TeamCompositionResult


def _grant(capability: str, tool_ids: list[str]) -> ToolGrant:
    return ToolGrant(capability=capability, tool_ids=tool_ids)


def _critic_validator(target_id: str, checks: list[str]) -> TeamAssignment:
    return TeamAssignment(
        id="critic-1",
        agent_template_id="critic",
        objective="Validate the producer output independently.",
        required_capabilities=["validation"],
        tool_grants=[_grant("validation", ["read_artifacts", "rubric_check"])],
        acceptance_checks=checks,
        validates_assignment_ids=[target_id],
    )


def _plan_for_scenario(scenario_id: str) -> TeamCompositionPlan:
    if scenario_id == "evidence-brief":
        return TeamCompositionPlan(
            task_summary="Prepare a cited evidence brief and validate it.",
            assignments=[
                TeamAssignment(
                    id="research-1",
                    agent_template_id="researcher",
                    objective="Gather cited evidence.",
                    required_capabilities=["evidence_gathering"],
                    tool_grants=[_grant("evidence_gathering", ["approved_web_lookup", "citation_lookup"])],
                    expected_artifacts=["brief"],
                ),
                _critic_validator("research-1", ["evidence_citations"]),
            ],
            work_graph=[
                WorkNode(id="node-research", assignment_id="research-1"),
                WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-research"]),
            ],
            selection_rationale="Research plus independent critique is sufficient.",
        )
    if scenario_id == "repository-repair":
        return TeamCompositionPlan(
            task_summary="Repair the backend fixture and validate the checks.",
            assignments=[
                TeamAssignment(
                    id="builder-1",
                    agent_template_id="builder",
                    objective="Implement the repair.",
                    required_capabilities=["implementation", "sandbox_execution"],
                    tool_grants=[_grant("implementation", [
                        "repository_write",
                        "start_execution_environment",
                        "execute_command",
                        "export_artifact",
                        "close_execution_environment",
                    ])],
                    owned_paths=["backend/service"],
                    expected_artifacts=["patch"],
                ),
                TeamAssignment(
                    id="test-1",
                    agent_template_id="test_engineer",
                    objective="Run the targeted checks independently.",
                    required_capabilities=["test_execution"],
                    tool_grants=[_grant("test_execution", [
                        "repository_read",
                        "start_execution_environment",
                        "execute_command",
                        "inspect_artifact",
                        "close_execution_environment",
                    ])],
                    acceptance_checks=["executed_checks"],
                    validates_assignment_ids=["builder-1"],
                ),
            ],
            work_graph=[
                WorkNode(id="node-build", assignment_id="builder-1", estimated_cost_class="medium"),
                WorkNode(id="node-test", assignment_id="test-1", depends_on=["node-build"]),
            ],
            selection_rationale="Builder then independent test engineer.",
        )
    if scenario_id == "screenshot-to-product":
        return TeamCompositionPlan(
            task_summary="Implement the UI and validate it independently.",
            assignments=[
                TeamAssignment(
                    id="frontend-1",
                    agent_template_id="frontend_engineer",
                    objective="Implement the UI.",
                    required_capabilities=["ui_engineering", "sandbox_execution"],
                    tool_grants=[_grant("ui_engineering", [
                        "repository_write",
                        "start_execution_environment",
                        "execute_command",
                        "browser_test",
                        "export_artifact",
                        "close_execution_environment",
                    ])],
                    owned_paths=["frontend/src"],
                    expected_artifacts=["ui-build"],
                ),
                TeamAssignment(
                    id="test-1",
                    agent_template_id="test_engineer",
                    objective="Validate the UI behavior independently.",
                    required_capabilities=["validation"],
                    tool_grants=[_grant("validation", [
                        "repository_read",
                        "start_execution_environment",
                        "execute_command",
                        "inspect_artifact",
                        "close_execution_environment",
                    ])],
                    acceptance_checks=["functional_validation"],
                    validates_assignment_ids=["frontend-1"],
                ),
            ],
            work_graph=[
                WorkNode(id="node-frontend", assignment_id="frontend-1", estimated_cost_class="medium"),
                WorkNode(id="node-test", assignment_id="test-1", depends_on=["node-frontend"]),
            ],
            selection_rationale="Frontend implementation plus independent validation.",
        )
    if scenario_id == "cross-media-launch":
        return TeamCompositionPlan(
            task_summary="Produce research, image, video, and critique.",
            assignments=[
                TeamAssignment(
                    id="research-1",
                    agent_template_id="researcher",
                    objective="Gather factual campaign evidence.",
                    required_capabilities=["evidence_gathering"],
                    tool_grants=[_grant("evidence_gathering", ["approved_web_lookup", "citation_lookup"])],
                    expected_artifacts=["brief"],
                ),
                TeamAssignment(
                    id="image-1",
                    agent_template_id="image_creator",
                    objective="Generate the social image.",
                    required_capabilities=["image_generation"],
                    tool_grants=[_grant("image_generation", ["generate_images", "inspect_image", "publish_image"])],
                    expected_artifacts=["image"],
                ),
                TeamAssignment(
                    id="video-1",
                    agent_template_id="video_producer",
                    objective="Generate the short video artifact.",
                    required_capabilities=["video_generation"],
                    tool_grants=[_grant("video_generation", ["submit_text_to_video", "get_video_job", "collect_video", "inspect_video"])],
                    expected_artifacts=["video"],
                ),
                TeamAssignment(
                    id="critic-1",
                    agent_template_id="critic",
                    objective="Validate the campaign package.",
                    required_capabilities=["validation"],
                    tool_grants=[_grant("validation", ["read_artifacts", "rubric_check"])],
                    acceptance_checks=["artifact_provenance"],
                    validates_assignment_ids=["image-1", "video-1"],
                ),
            ],
            work_graph=[
                WorkNode(id="node-research", assignment_id="research-1"),
                WorkNode(id="node-image", assignment_id="image-1", estimated_cost_class="medium"),
                WorkNode(id="node-video", assignment_id="video-1", estimated_cost_class="medium"),
                WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-research", "node-image", "node-video"]),
            ],
            selection_rationale="Parallel research and media, then critique.",
        )
    if scenario_id == "data-investigation":
        return TeamCompositionPlan(
            task_summary="Investigate the dataset and validate the findings.",
            assignments=[
                TeamAssignment(
                    id="data-1",
                    agent_template_id="data_analyst",
                    objective="Analyze the anomaly.",
                    required_capabilities=["data_analysis"],
                    tool_grants=[_grant("data_analysis", ["start_execution_environment", "run_code", "export_artifact", "close_execution_environment"])],
                    expected_artifacts=["analysis"],
                ),
                TeamAssignment(
                    id="research-1",
                    agent_template_id="researcher",
                    objective="Provide domain research context.",
                    required_capabilities=["evidence_gathering"],
                    tool_grants=[_grant("evidence_gathering", ["approved_web_lookup", "citation_lookup"])],
                    expected_artifacts=["notes"],
                ),
                _critic_validator("data-1", ["independent_validation"]),
            ],
            work_graph=[
                WorkNode(id="node-data", assignment_id="data-1", estimated_cost_class="medium"),
                WorkNode(id="node-research", assignment_id="research-1"),
                WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-data", "node-research"]),
            ],
            selection_rationale="Analysis plus domain research plus critique.",
        )
    if scenario_id == "small-bounded-task":
        return TeamCompositionPlan(
            task_summary="Answer the bounded evidence question.",
            assignments=[
                TeamAssignment(
                    id="research-1",
                    agent_template_id="researcher",
                    objective="Answer the small question.",
                    required_capabilities=["evidence_gathering"],
                    tool_grants=[_grant("evidence_gathering", ["approved_web_lookup"])],
                ),
            ],
            work_graph=[WorkNode(id="node-research", assignment_id="research-1")],
            selection_rationale="One researcher is enough.",
        )
    raise KeyError(scenario_id)


def _officialize_trial(record, *, provider_preflight: dict[str, list[dict[str, str]]] | None = None):
    """Project a test record into sealed official metadata for its scenario."""

    public_scenario, _private_label = load_scenario_pair(record.scenario_id)
    preflight = provider_preflight or {"typed_blockers": []}
    return record.model_copy(update={
        "mode": "official",
        "official": True,
        "model": FROZEN_OFFICIAL_CONFIG.model,
        "provider": FROZEN_OFFICIAL_CONFIG.provider,
        "limits": public_scenario.limits,
        "attempt_ceiling": public_scenario.attempt_ceiling,
        "available_tool_ids": list(public_scenario.available_tool_ids or []),
        "available_agent_template_ids": list(public_scenario.available_agent_template_ids or []),
        "provider_preflight": preflight,
        "public_scenario_hash": public_scenario.public_hash,
        "public_scenario_ref": f"public_suite:{public_scenario.scenario_id}",
        "injected_unavailable_template_id": public_scenario.injected_unavailable_template_id,
        "scenario_family": public_scenario.family,
        "scenario_title": public_scenario.title,
    })


def _build_official_suite_trials(*, include_mixed_outcomes: bool = False):
    """Build one retained official record per public scenario without live providers."""

    public_suite, _public_hash = load_public_suite()
    official_preflight = {"typed_blockers": []}
    with tempfile.TemporaryDirectory() as temp_dir:
        output_dir = Path(temp_dir)
        trials = []
        for scenario in public_suite:
            if scenario.scenario_id == "ambiguous-high-impact":
                composer = _FakeComposer(TeamCompositionBlocked(
                    category="missing_user_input",
                    message="Need clarification.",
                    issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need clarification")],
                ))
            elif include_mixed_outcomes and scenario.scenario_id == "data-investigation":
                composer = _FakeComposer(RuntimeError("provider failure"))
            else:
                composer = _FakeComposer(TeamCompositionResult(
                    plan=_plan_for_scenario(scenario.scenario_id),
                    attempt_count=1,
                    recomposed=False,
                    validation_issue_history=[],
                ))
            try:
                record = run_trial_sync(
                    scenario_id=scenario.scenario_id,
                    output_dir=output_dir,
                    mode="development",
                    model="fake-model",
                    provider="fake-provider",
                    composer=composer,
                )
            except RuntimeError:
                record = next(
                    candidate
                    for candidate in load_evaluated_trials(output_dir)
                    if candidate.scenario_id == scenario.scenario_id
                )
            trials.append(_officialize_trial(record, provider_preflight=official_preflight))
    return trials, official_preflight


class _FakeComposer:
    def __init__(self, result: TeamCompositionResult | TeamCompositionBlocked | BaseException) -> None:
        self.result = result
        self.contexts: list[CompositionContext] = []

    async def compose(self, context: CompositionContext) -> TeamCompositionResult:
        self.contexts.append(context.model_copy(deep=True))
        if isinstance(self.result, BaseException):
            raise self.result
        if isinstance(self.result, TeamCompositionBlocked):
            raise self.result
        return self.result


class BenchmarkTeamCompositionTests(unittest.TestCase):
    def test_all_sealed_families_load_and_hashes_are_stable(self) -> None:
        public_suite, public_hash = load_public_suite()
        private_suite, private_hash = load_private_suite()

        self.assertEqual([scenario.family for scenario in public_suite], [
            "evidence_brief",
            "repository_repair",
            "screenshot_to_product",
            "cross_media_launch",
            "data_investigation",
            "ambiguous_high_impact",
            "small_bounded_task",
        ])
        self.assertEqual(len(private_suite), 7)
        self.assertEqual(public_hash, "b076c491bf4f4b06282993396f06bae1db1651747786e31e9636f0e4dc84bffe")
        self.assertEqual(private_hash, "45d35934a4dd6ece8afffef4c11743b6eda8dc70dca684007d5664f5df1feb09")

    def test_public_suite_has_explicit_registry_backed_availability(self) -> None:
        public_suite, _public_hash = load_public_suite()
        for scenario in public_suite:
            self.assertIsNotNone(scenario.available_tool_ids)
            self.assertIsNotNone(scenario.available_agent_template_ids)
            self.assertEqual(
                scenario.available_tool_ids,
                sorted(dict.fromkeys(scenario.available_tool_ids)),
            )
            self.assertEqual(
                scenario.available_agent_template_ids,
                list(dict.fromkeys(scenario.available_agent_template_ids)),
            )
        data_investigation = next(
            scenario for scenario in public_suite if scenario.scenario_id == "data-investigation"
        )
        self.assertNotIn("data_analyst", data_investigation.available_agent_template_ids)
        self.assertEqual(data_investigation.injected_unavailable_template_id, "data_analyst")

    def test_private_labels_never_reach_context_or_public_record(self) -> None:
        public_scenario, private_label = load_scenario_pair("repository-repair")
        composer = _FakeComposer(TeamCompositionResult(
            plan=_plan_for_scenario("repository-repair"),
            attempt_count=1,
            recomposed=False,
            validation_issue_history=[],
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_trial_sync(
                scenario_id=public_scenario.scenario_id,
                output_dir=Path(temp_dir),
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
            serialized_context = composer.contexts[0].model_dump_json()
            self.assertNotIn(private_label.private_hash, serialized_context)
            self.assertNotIn("forbidden_tool_grants", serialized_context)
            public_path = Path(temp_dir) / public_scenario.scenario_id / f"{record.trial_id}.public.json"
            serialized_public = public_path.read_text(encoding="utf-8")
            self.assertNotIn(private_label.private_hash, serialized_public)
            self.assertNotIn("required_capabilities", serialized_public)

    def test_perfect_plan_metrics_and_strict_pass(self) -> None:
        public_scenario, private_label = load_scenario_pair("repository-repair")
        evaluation = evaluate_plan(
            _plan_for_scenario("repository-repair"),
            private_label,
            available_tool_ids=["repository_write", "repository_read", "start_execution_environment", "execute_command", "inspect_artifact", "export_artifact", "close_execution_environment"],
            available_agent_template_ids=["builder", "test_engineer", "critic", "coordinator"],
            limits=public_scenario.limits,
        )
        self.assertTrue(evaluation.strict_pass)
        self.assertEqual(evaluation.outcome_type, "plan")
        self.assertTrue(all(metric.passed for metric in evaluation.metrics[:8]))

    def test_missing_required_capability_is_reported(self) -> None:
        _public_scenario, private_label = load_scenario_pair("repository-repair")
        bad_plan = _plan_for_scenario("repository-repair")
        bad_plan.assignments[1].required_capabilities = []
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["repository_write", "repository_read", "start_execution_environment", "execute_command", "inspect_artifact", "export_artifact", "close_execution_environment"],
            available_agent_template_ids=["builder", "test_engineer", "critic"],
            limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=6),
        )
        self.assertIn("unowned_required_capability", evaluation.validation_issue_codes)

    def test_registry_backed_recall_does_not_credit_self_declared_capability(self) -> None:
        _public_scenario, private_label = load_scenario_pair("data-investigation")
        bad_plan = TeamCompositionPlan(
            task_summary="Use the wrong role for analysis.",
            assignments=[
                TeamAssignment(
                    id="research-1",
                    agent_template_id="researcher",
                    objective="Pretend to analyze the data.",
                    required_capabilities=["data_analysis"],
                    tool_grants=[_grant("data_analysis", ["approved_web_lookup"])],
                    expected_artifacts=["analysis"],
                ),
                _critic_validator("research-1", ["independent_validation"]),
            ],
            work_graph=[
                WorkNode(id="node-research", assignment_id="research-1"),
                WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-research"]),
            ],
            selection_rationale="This intentionally self-declares the wrong capability.",
        )
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["approved_web_lookup", "read_artifacts", "rubric_check"],
            available_agent_template_ids=["researcher", "critic"],
            limits=CompositionLimits(max_dynamic_specialists=3, max_total_cost_units=4),
        )
        metric = next(metric for metric in evaluation.metrics if metric.name == "required_capability_recall")
        self.assertFalse(metric.passed)
        self.assertIn("data_analysis", metric.detail["missing"])
        self.assertNotIn("data_analysis", metric.detail["selected"])

    def test_unnecessary_specialist_limit_is_reported(self) -> None:
        _public_scenario, private_label = load_scenario_pair("small-bounded-task")
        bad_plan = _plan_for_scenario("small-bounded-task")
        bad_plan.assignments.append(TeamAssignment(id="critic-1", agent_template_id="critic", objective="Unnecessary review.", required_capabilities=["validation"], tool_grants=[_grant("validation", ["read_artifacts"])]))
        bad_plan.assignments.append(TeamAssignment(id="architect-1", agent_template_id="architect", objective="Unnecessary design.", required_capabilities=["architecture"], tool_grants=[_grant("architecture", ["dependency_graph"])]))
        bad_plan.work_graph.extend([
            WorkNode(id="node-critic", assignment_id="critic-1"),
            WorkNode(id="node-architect", assignment_id="architect-1"),
        ])
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["approved_web_lookup", "read_artifacts", "dependency_graph"],
            available_agent_template_ids=["researcher", "critic", "architect"],
            limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=6),
        )
        metric = next(metric for metric in evaluation.metrics if metric.name == "unnecessary_capability_rate_count")
        self.assertFalse(metric.passed)
        self.assertEqual(metric.value, 2)

    def test_forbidden_grants_are_reported(self) -> None:
        _public_scenario, private_label = load_scenario_pair("evidence-brief")
        bad_plan = _plan_for_scenario("evidence-brief")
        bad_plan.assignments[0].tool_grants = [_grant("evidence_gathering", ["approved_web_lookup", "execute_command"])]
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["approved_web_lookup", "execute_command", "read_artifacts", "rubric_check"],
            available_agent_template_ids=["researcher", "critic"],
            limits=CompositionLimits(max_dynamic_specialists=3, max_total_cost_units=4),
        )
        metric = next(metric for metric in evaluation.metrics if metric.name == "forbidden_grant_violations")
        self.assertFalse(metric.passed)
        self.assertIn("execute_command", metric.detail["forbidden_grant_tool_ids"])

    def test_assignment_tool_incompatibility_is_reported(self) -> None:
        _public_scenario, private_label = load_scenario_pair("repository-repair")
        bad_plan = _plan_for_scenario("repository-repair")
        bad_plan.assignments[0].tool_grants = [_grant("implementation", ["repository_write"])]
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["repository_write", "repository_read", "start_execution_environment", "execute_command", "inspect_artifact", "export_artifact", "close_execution_environment"],
            available_agent_template_ids=["builder", "test_engineer"],
            limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=6),
        )
        metric = next(metric for metric in evaluation.metrics if metric.name == "assignment_tool_compatibility")
        self.assertFalse(metric.passed)
        self.assertIn("missing_required_tool", metric.detail["issue_codes"])

    def test_missing_independent_validator_is_reported(self) -> None:
        _public_scenario, private_label = load_scenario_pair("repository-repair")
        bad_plan = _plan_for_scenario("repository-repair")
        bad_plan.assignments = bad_plan.assignments[:1]
        bad_plan.work_graph = bad_plan.work_graph[:1]
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["repository_write", "start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
            available_agent_template_ids=["builder"],
            limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=6),
        )
        metric = next(metric for metric in evaluation.metrics if metric.name == "independent_validator_coverage")
        self.assertFalse(metric.passed)
        self.assertIn("builder", metric.detail["missing_target_template_ids"])

    def test_bad_dependencies_and_writer_conflicts_are_reported(self) -> None:
        _public_scenario, private_label = load_scenario_pair("repository-repair")
        bad_plan = _plan_for_scenario("repository-repair")
        bad_plan.assignments.append(
            TeamAssignment(
                id="builder-2",
                agent_template_id="builder",
                objective="Competing edit.",
                required_capabilities=["implementation", "sandbox_execution"],
                tool_grants=[_grant("implementation", ["repository_write", "start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"])],
                owned_paths=["backend/service/shared.py"],
                expected_artifacts=["patch-2"],
            )
        )
        bad_plan.work_graph = [
            WorkNode(id="node-build-1", assignment_id="builder-1"),
            WorkNode(id="node-build-2", assignment_id="builder-2"),
            WorkNode(id="node-test", assignment_id="test-1"),
        ]
        evaluation = evaluate_plan(
            bad_plan,
            private_label,
            available_tool_ids=["repository_write", "repository_read", "start_execution_environment", "execute_command", "inspect_artifact", "export_artifact", "close_execution_environment"],
            available_agent_template_ids=["builder", "test_engineer"],
            limits=CompositionLimits(max_dynamic_specialists=4, max_total_cost_units=6),
        )
        dependency_metric = next(metric for metric in evaluation.metrics if metric.name == "dependency_correctness")
        conflict_metric = next(metric for metric in evaluation.metrics if metric.name == "conflict_correctness")
        self.assertFalse(dependency_metric.passed)
        self.assertFalse(conflict_metric.passed)

    def test_correct_and_incorrect_typed_blocker_are_scored(self) -> None:
        _public_scenario, private_label = load_scenario_pair("ambiguous-high-impact")
        good_blocked = TeamCompositionBlocked(
            category="missing_user_input",
            message="Need user clarification.",
            issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need acceptance checks")],
        )
        bad_blocked = TeamCompositionBlocked(
            category="missing_system_capability",
            message="Incorrect blocker category.",
            issues=[PlanValidationIssue(code="missing_required_tool", message="Not the user blocker path")],
        )
        self.assertTrue(evaluate_blocked(good_blocked, private_label).strict_pass)
        self.assertFalse(evaluate_blocked(bad_blocked, private_label).strict_pass)

    def test_unavailable_specialist_recovery_bookkeeping_is_reported(self) -> None:
        public_scenario, private_label = load_scenario_pair("data-investigation")
        blocked = TeamCompositionBlocked(
            category="missing_system_capability",
            message="Unavailable specialist cannot be recovered.",
            issues=[PlanValidationIssue(code="unavailable_agent_template", message="data_analyst unavailable")],
        )
        evaluation = evaluate_blocked(blocked, private_label)
        typed_metric = next(metric for metric in evaluation.metrics if metric.name == "typed_blocker_correctness")
        metric = next(metric for metric in evaluation.metrics if metric.name == "recovery_after_unavailable_specialist")
        self.assertTrue(evaluation.strict_pass)
        self.assertTrue(typed_metric.passed)
        self.assertEqual(typed_metric.detail["expected_blocker_category"], "missing_system_capability")
        self.assertTrue(metric.passed)
        self.assertEqual(public_scenario.injected_unavailable_template_id, "data_analyst")

    def test_blocked_recomposition_count_is_persisted_after_bounded_retry(self) -> None:
        blocked = TeamCompositionBlocked(
            category="missing_system_capability",
            message="Plan remained invalid after recomposition.",
            issues=[PlanValidationIssue(code="missing_required_tool", message="builder grant incomplete")],
            attempt_count=2,
            validation_issue_history=[
                [PlanValidationIssue(code="missing_required_tool", message="attempt one invalid")],
                [PlanValidationIssue(code="missing_required_tool", message="attempt two invalid")],
            ],
        )
        composer = _FakeComposer(blocked)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            record = run_trial_sync(
                scenario_id="repository-repair",
                output_dir=output_dir,
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
            self.assertEqual(record.recomposition_count, 1)
            persisted = load_evaluated_trials(output_dir)[0]
            self.assertEqual(persisted.recomposition_count, 1)
            self.assertEqual(len(persisted.validation_issue_history), 2)

    def test_event_log_is_durably_persisted_for_success_blocked_and_failed_trials(self) -> None:
        cases = [
            (
                "success",
                _FakeComposer(TeamCompositionResult(
                    plan=_plan_for_scenario("small-bounded-task"),
                    attempt_count=1,
                    recomposed=False,
                    validation_issue_history=[],
                )),
            ),
            (
                "blocked",
                _FakeComposer(TeamCompositionBlocked(
                    category="missing_user_input",
                    message="Need clarification.",
                    issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need clarification")],
                )),
            ),
            ("failed", _FakeComposer(RuntimeError("provider secret=sk-live-123 failed"))),
        ]
        for label, composer in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temp_dir:
                    output_dir = Path(temp_dir)
                    if label == "failed":
                        with self.assertRaises(RuntimeError):
                            run_trial_sync(
                                scenario_id="small-bounded-task",
                                output_dir=output_dir,
                                mode="development",
                                model="fake-model",
                                provider="fake-provider",
                                composer=composer,
                            )
                    else:
                        run_trial_sync(
                            scenario_id="small-bounded-task",
                            output_dir=output_dir,
                            mode="development",
                            model="fake-model",
                            provider="fake-provider",
                            composer=composer,
                        )
                    persisted = load_evaluated_trials(output_dir)[0]
                    self.assertEqual(
                        persisted.event_log,
                        [{"public_record": f"{persisted.scenario_id}/{persisted.trial_id}.public.json"}],
                    )

    def test_usage_and_timings_are_truthful_when_provider_metrics_are_unavailable(self) -> None:
        composer = _FakeComposer(TeamCompositionResult(
            plan=_plan_for_scenario("small-bounded-task"),
            attempt_count=1,
            recomposed=False,
            validation_issue_history=[],
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_trial_sync(
                scenario_id="small-bounded-task",
                output_dir=Path(temp_dir),
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
        self.assertEqual(
            record.usage,
            {
                "usage_complete": False,
                "unavailable_reason": "composer/provider usage metrics not exposed by current team composition surface",
            },
        )
        self.assertIn("compose_wall_seconds", record.timings)
        self.assertGreaterEqual(record.timings["compose_wall_seconds"], 0.0)

    def test_json_persistence_replay_and_no_silent_overwrite(self) -> None:
        composer = _FakeComposer(TeamCompositionResult(plan=_plan_for_scenario("small-bounded-task"), attempt_count=1, recomposed=False, validation_issue_history=[]))
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            first = run_trial_sync(
                scenario_id="small-bounded-task",
                output_dir=output_dir,
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
            second = run_trial_sync(
                scenario_id="small-bounded-task",
                output_dir=output_dir,
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
            loaded = load_evaluated_trials(output_dir)
            self.assertEqual([record.trial_id for record in loaded], [first.trial_id, second.trial_id])
            self.assertNotEqual(first.trial_id, second.trial_id)
            with self.assertRaises(FileExistsError):
                persist_evaluated_trial(output_dir, loaded[0])

    def test_aggregate_report_reconstruction(self) -> None:
        composer = _FakeComposer(TeamCompositionResult(plan=_plan_for_scenario("repository-repair"), attempt_count=1, recomposed=False, validation_issue_history=[]))
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_trial_sync(
                scenario_id="repository-repair",
                output_dir=Path(temp_dir),
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
            report = aggregate_trials([record], mode="development")
            self.assertEqual(report.scenario_reports[0].scenario_id, "repository-repair")
            self.assertEqual(report.scenario_reports[0].attempts, 1)
            self.assertEqual(report.scenario_reports[0].strict_passes, 1)

    def test_official_freeze_accepts_each_public_scenario_and_preserves_preflight_guards(self) -> None:
        public_suite, _public_hash = load_public_suite()
        for scenario in public_suite:
            validate_official_freeze(
                mode="official",
                model=FROZEN_OFFICIAL_CONFIG.model,
                provider=FROZEN_OFFICIAL_CONFIG.provider,
                limits=scenario.limits,
                attempt_ceiling=scenario.attempt_ceiling,
                available_tool_ids=scenario.available_tool_ids,
                available_agent_template_ids=scenario.available_agent_template_ids,
                suite_version=SUITE_VERSION,
                evaluator_version=EVALUATOR_VERSION,
                provider_preflight={"typed_blockers": []},
                scenario=scenario,
                scenario_family=scenario.family,
                scenario_title=scenario.title,
                public_scenario_hash=scenario.public_hash,
                public_scenario_ref=f"public_suite:{scenario.scenario_id}",
                injected_unavailable_template_id=scenario.injected_unavailable_template_id,
            )
        with self.assertRaises(ValueError):
            validate_official_freeze(
                mode="official",
                model=FROZEN_OFFICIAL_CONFIG.model,
                provider=FROZEN_OFFICIAL_CONFIG.provider,
                limits=FROZEN_OFFICIAL_CONFIG.limits,
                attempt_ceiling=FROZEN_OFFICIAL_CONFIG.attempt_ceiling,
                available_tool_ids=["approved_web_lookup"],
                available_agent_template_ids=["researcher"],
                suite_version=SUITE_VERSION,
                evaluator_version=EVALUATOR_VERSION,
                provider_preflight=None,
            )
        with self.assertRaises(ValueError):
            validate_official_freeze(
                mode="official",
                model="not-qwen",
                provider=FROZEN_OFFICIAL_CONFIG.provider,
                limits=FROZEN_OFFICIAL_CONFIG.limits,
                attempt_ceiling=FROZEN_OFFICIAL_CONFIG.attempt_ceiling,
                available_tool_ids=["approved_web_lookup"],
                available_agent_template_ids=["researcher"],
                suite_version=SUITE_VERSION,
                evaluator_version=EVALUATOR_VERSION,
                provider_preflight={"typed_blockers": []},
            )
        with self.assertRaises(ValueError):
            validate_official_freeze(
                mode="official",
                model=FROZEN_OFFICIAL_CONFIG.model,
                provider=FROZEN_OFFICIAL_CONFIG.provider,
                limits=FROZEN_OFFICIAL_CONFIG.limits,
                attempt_ceiling=FROZEN_OFFICIAL_CONFIG.attempt_ceiling,
                available_tool_ids=["approved_web_lookup"],
                available_agent_template_ids=["researcher"],
                suite_version=SUITE_VERSION,
                evaluator_version=EVALUATOR_VERSION,
                provider_preflight={"typed_blockers": [{"code": "agentbay_api_key_missing"}]},
            )

    def test_public_runtime_availability_is_explicit_and_does_not_reject_valid_grants(self) -> None:
        composer = _FakeComposer(TeamCompositionResult(plan=_plan_for_scenario("small-bounded-task"), attempt_count=1, recomposed=False, validation_issue_history=[]))
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_trial_sync(
                scenario_id="small-bounded-task",
                output_dir=Path(temp_dir),
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=composer,
            )
            self.assertEqual(
                composer.contexts[0].available_tool_ids,
                load_scenario_pair("small-bounded-task")[0].available_tool_ids,
            )
            self.assertEqual(
                composer.contexts[0].available_agent_template_ids,
                load_scenario_pair("small-bounded-task")[0].available_agent_template_ids,
            )
            self.assertTrue(record.evaluation.strict_pass)

    def test_official_aggregate_requires_exact_suite_coverage_and_retains_mixed_evidence(self) -> None:
        trials, official_preflight = _build_official_suite_trials(include_mixed_outcomes=True)
        report = aggregate_trials(trials, mode="official", provider_preflight=official_preflight)
        by_id = {scenario.scenario_id: scenario for scenario in report.scenario_reports}
        self.assertEqual(len(by_id), 7)
        self.assertEqual(by_id["ambiguous-high-impact"].blocked, 1)
        self.assertEqual(by_id["data-investigation"].failed, 1)
        self.assertEqual(by_id["small-bounded-task"].successes, 1)

    def test_official_aggregate_refuses_duplicate_scenarios(self) -> None:
        trials, official_preflight = _build_official_suite_trials()
        with self.assertRaises(ValueError):
            aggregate_trials(
                trials + [trials[0].model_copy(deep=True)],
                mode="official",
                provider_preflight=official_preflight,
            )

    def test_official_aggregate_refuses_missing_scenarios(self) -> None:
        trials, official_preflight = _build_official_suite_trials()
        with self.assertRaises(ValueError):
            aggregate_trials(
                trials[:-1],
                mode="official",
                provider_preflight=official_preflight,
            )

    def test_official_aggregate_refuses_tampered_public_metadata(self) -> None:
        trials, official_preflight = _build_official_suite_trials()
        tampered = list(trials)
        tampered[0] = tampered[0].model_copy(update={"available_tool_ids": tampered[0].available_tool_ids[:-1]})
        with self.assertRaises(ValueError):
            aggregate_trials(
                tampered,
                mode="official",
                provider_preflight=official_preflight,
            )

    def test_official_cli_refuses_partial_or_nonempty_output_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "stale.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                asyncio.run(benchmark_cli._run(argparse.Namespace(
                    scenario="all",
                    mode="official",
                    output_dir=str(output_dir),
                )))
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                asyncio.run(benchmark_cli._run(argparse.Namespace(
                    scenario="small-bounded-task",
                    mode="official",
                    output_dir=temp_dir,
                )))

    def test_official_cli_uses_empty_output_directory_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(
                scenario="all",
                mode="official",
                output_dir=temp_dir,
            )
            with (
                mock.patch.object(benchmark_cli, "get_settings", return_value=object()),
                mock.patch.object(benchmark_cli, "model_capability_preflight", return_value={"typed_blockers": []}),
                mock.patch.object(benchmark_cli, "run_live_composition_trial", new=mock.AsyncMock()),
                mock.patch.object(benchmark_cli, "write_aggregate_bundle"),
            ):
                asyncio.run(benchmark_cli._run(args))
                self.assertEqual(benchmark_cli.run_live_composition_trial.await_count, 7)
                benchmark_cli.write_aggregate_bundle.assert_called_once()

    def test_failed_and_excluded_attempts_are_retained(self) -> None:
        success_composer = _FakeComposer(TeamCompositionResult(plan=_plan_for_scenario("small-bounded-task"), attempt_count=1, recomposed=False, validation_issue_history=[]))
        blocked_composer = _FakeComposer(TeamCompositionBlocked(
            category="missing_user_input",
            message="Need clarification.",
            issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need clarification")],
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            success = run_trial_sync(
                scenario_id="small-bounded-task",
                output_dir=Path(temp_dir),
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=success_composer,
            )
            blocked = run_trial_sync(
                scenario_id="ambiguous-high-impact",
                output_dir=Path(temp_dir),
                mode="development",
                model="fake-model",
                provider="fake-provider",
                composer=blocked_composer,
            )
            report = aggregate_trials([success, blocked], mode="development")
            by_id = {scenario.scenario_id: scenario for scenario in report.scenario_reports}
            self.assertEqual(by_id["small-bounded-task"].successes, 1)
            self.assertEqual(by_id["ambiguous-high-impact"].blocked, 1)

    def test_unexpected_failures_are_persisted_then_re_raised(self) -> None:
        failing_composer = _FakeComposer(RuntimeError("provider auth failed for sk-live-123"))
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with self.assertRaises(RuntimeError):
                run_trial_sync(
                    scenario_id="small-bounded-task",
                    output_dir=output_dir,
                    mode="development",
                    model="fake-model",
                    provider="fake-provider",
                    composer=failing_composer,
                )
            loaded = load_evaluated_trials(output_dir)
            self.assertEqual(len(loaded), 1)
            failed = loaded[0]
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.evaluation.outcome_type, "failed")
            self.assertIsNone(failed.selected_plan)
            self.assertIsNotNone(failed.failure_detail)
            self.assertEqual(failed.failure_detail.error_type, "RuntimeError")
            self.assertIn("failed", failed.failure_detail.message)
            self.assertNotIn("sk-live-123", failed.failure_detail.message)
            self.assertEqual(failed.evaluation.metrics, [])


if __name__ == "__main__":
    unittest.main()
