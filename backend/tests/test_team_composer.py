"""Focused tests for Phase 4 typed team composition and recomposition."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.capability_registry import get_role_capabilities, list_specialist_templates
from society.schemas.team_composition import (
    CompositionLimits,
    TeamAssignment,
    TeamCompositionPlan,
    ToolGrant,
    WorkNode,
)
from society.team_composer import (
    TEAM_COMPOSITION_BLOCKED,
    AgnoTeamPlanProvider,
    CatalogRoleSnapshot,
    CompositionContext,
    TeamComposer,
    TeamCompositionBlocked,
    TeamCompositionProviderError,
    build_catalog_snapshot,
)


def _valid_plan() -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Implement a narrow backend change with independent validation.",
        assignments=[
            TeamAssignment(
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
                owned_paths=["backend/society/team_composer.py"],
                expected_artifacts=["patch"],
            ),
            TeamAssignment(
                id="critic-1",
                agent_template_id="critic",
                objective="Independently validate the implementation",
                required_capabilities=["validation"],
                tool_grants=[ToolGrant(capability="validation", tool_ids=["read_artifacts", "rubric_check"])],
                acceptance_checks=["artifact_diff_review", "regression_checks"],
                validates_assignment_ids=["builder-1"],
            ),
        ],
        work_graph=[
            WorkNode(id="node-build", assignment_id="builder-1", estimated_cost_class="medium"),
            WorkNode(id="node-critic", assignment_id="critic-1", depends_on=["node-build"]),
        ],
        selection_rationale="One builder and one independent validator are sufficient.",
    )


def _invalid_tool_plan() -> TeamCompositionPlan:
    plan = _valid_plan()
    plan.assignments[0].tool_grants = [
        ToolGrant(capability="implementation", tool_ids=["repository_write"])
    ]
    plan.selection_rationale = "This intentionally omits required execution tools."
    return plan


def _acceptance_gap_plan() -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Generate an image that still needs acceptance detail.",
        assignments=[
            TeamAssignment(
                id="image-1",
                agent_template_id="image_creator",
                objective="Generate the requested image artifact",
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
        selection_rationale="The acceptance requirements are still incomplete.",
    )


class _RecordingProvider:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def propose(
        self,
        context: CompositionContext,
        catalog_snapshot: list[CatalogRoleSnapshot],
        previous_issue_codes: list[str],
        attempt_number: int,
    ) -> TeamCompositionPlan:
        self.calls.append({
            "task_id": context.task_id,
            "catalog_snapshot": catalog_snapshot,
            "previous_issue_codes": list(previous_issue_codes),
            "attempt_number": attempt_number,
        })
        if not self._responses:
            raise AssertionError("provider called more times than expected")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeAgent:
    def __init__(self, response: object, prompt_log: list[str], kwargs_log: list[dict[str, object]]) -> None:
        self._response = response
        self._prompt_log = prompt_log
        self._kwargs_log = kwargs_log

    async def run(self, prompt: str) -> object:
        self._prompt_log.append(prompt)
        return self._response


class TeamComposerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_first_plan_emits_proposed_and_validated_without_recomposition(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        provider = _RecordingProvider([_valid_plan()])
        composer = TeamComposer(provider, lambda event_type, payload: events.append((event_type, dict(payload))))

        result = await composer.compose(_context())

        self.assertEqual(result.attempt_count, 1)
        self.assertFalse(result.recomposed)
        self.assertEqual(result.validation_issue_history, [])
        self.assertEqual([event[0] for event in events], [
            "team_composition_proposed",
            "team_composition_validated",
        ])
        self.assertEqual(provider.calls[0]["previous_issue_codes"], [])

    async def test_invalid_capability_or_tool_plan_recomposes_once_then_succeeds(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        provider = _RecordingProvider([_invalid_tool_plan(), _valid_plan()])
        composer = TeamComposer(provider, lambda event_type, payload: events.append((event_type, dict(payload))))

        result = await composer.compose(_context())

        self.assertEqual(result.attempt_count, 2)
        self.assertTrue(result.recomposed)
        self.assertEqual(len(result.validation_issue_history), 1)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[1]["previous_issue_codes"], ["missing_required_tool"])
        self.assertEqual([event[0] for event in events], [
            "team_composition_proposed",
            "team_recomposition_requested",
            "team_composition_proposed",
            "team_composition_validated",
        ])
        self.assertEqual(events[1][1]["issue_codes"], ["missing_required_tool"])

    async def test_second_invalid_plan_stops_and_never_calls_provider_a_third_time(self) -> None:
        provider = _RecordingProvider([_invalid_tool_plan(), _invalid_tool_plan()])
        composer = TeamComposer(provider)

        with self.assertRaises(TeamCompositionBlocked) as exc:
            await composer.compose(_context())

        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(exc.exception.pause_for_user)
        self.assertEqual(exc.exception.category, "missing_system_capability")
        self.assertEqual(len(exc.exception.validation_issue_history), 2)

    async def test_provider_failure_stops_without_recomposition(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        provider = _RecordingProvider([
            TeamCompositionProviderError("provider_malformed_response", "bad payload"),
        ])
        composer = TeamComposer(provider, lambda event_type, payload: events.append((event_type, dict(payload))))

        with self.assertRaises(TeamCompositionBlocked) as exc:
            await composer.compose(_context())

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(exc.exception.category, "missing_system_capability")
        self.assertEqual([event[0] for event in events], [TEAM_COMPOSITION_BLOCKED])
        self.assertEqual(events[0][1]["issue_codes"], ["provider_malformed_response"])

    async def test_missing_user_acceptance_requirement_only_maps_to_user_input_when_explicit(self) -> None:
        provider = _RecordingProvider([_acceptance_gap_plan(), _acceptance_gap_plan()])
        composer = TeamComposer(provider)
        acceptance_context = _context(unresolved_user_requirements=["Need final image acceptance criteria"])
        acceptance_context.required_capabilities = ["image_generation", "validation"]

        with self.assertRaises(TeamCompositionBlocked) as user_exc:
            await composer.compose(acceptance_context)
        self.assertEqual(user_exc.exception.category, "missing_user_input")

        provider = _RecordingProvider([_acceptance_gap_plan(), _acceptance_gap_plan()])
        composer = TeamComposer(provider)
        acceptance_context = _context()
        acceptance_context.required_capabilities = ["image_generation", "validation"]
        with self.assertRaises(TeamCompositionBlocked) as system_exc:
            await composer.compose(acceptance_context)
        self.assertEqual(system_exc.exception.category, "missing_system_capability")

    async def test_event_payloads_remain_secret_safe(self) -> None:
        secret_text = "API_KEY=sk-live-123 prompt=super-secret objective=deploy-now"
        events: list[tuple[str, dict[str, object]]] = []
        provider = _RecordingProvider([_valid_plan()])
        composer = TeamComposer(provider, lambda event_type, payload: events.append((event_type, dict(payload))))

        await composer.compose(_context(task_summary=secret_text, user_request=secret_text))

        serialized = json.dumps(events)
        self.assertNotIn("API_KEY", serialized)
        self.assertNotIn("sk-live-123", serialized)
        self.assertNotIn("prompt=", serialized)
        self.assertNotIn("objective=", serialized)

    async def test_sync_and_async_event_sinks_are_supported(self) -> None:
        sync_events: list[str] = []
        sync_provider = _RecordingProvider([_valid_plan()])
        sync_composer = TeamComposer(sync_provider, lambda event_type, _payload: sync_events.append(event_type))
        await sync_composer.compose(_context(task_id="sync"))
        self.assertEqual(sync_events, ["team_composition_proposed", "team_composition_validated"])

        async_events: list[str] = []

        async def async_sink(event_type: str, _payload: dict[str, object]) -> None:
            async_events.append(event_type)

        async_provider = _RecordingProvider([_valid_plan()])
        async_composer = TeamComposer(async_provider, async_sink)
        await async_composer.compose(_context(task_id="async"))
        self.assertEqual(async_events, ["team_composition_proposed", "team_composition_validated"])


class CatalogSnapshotTests(unittest.TestCase):
    def test_catalog_snapshot_is_canonical_distinct_and_non_mutating(self) -> None:
        before = {
            role_id: registration.model_dump(mode="json")
            for role_id, registration in list_specialist_templates().items()
        }

        snapshot = build_catalog_snapshot()

        after = {
            role_id: registration.model_dump(mode="json")
            for role_id, registration in list_specialist_templates().items()
        }
        self.assertEqual(before, after)
        self.assertEqual(len(snapshot), 10)
        self.assertEqual(
            {entry.agent_template_id for entry in snapshot},
            {
                "architect",
                "builder",
                "coordinator",
                "critic",
                "data_analyst",
                "frontend_engineer",
                "image_creator",
                "researcher",
                "test_engineer",
                "video_producer",
            },
        )
        for entry in snapshot:
            self.assertEqual(len(entry.allowed_tool_ids), len(set(entry.allowed_tool_ids)))
            self.assertEqual(len(entry.required_tool_ids), len(set(entry.required_tool_ids)))
            self.assertNotIn("agentbay_execute", entry.allowed_tool_ids)
            self.assertNotIn("agentbay_export_artifact", entry.allowed_tool_ids)


class AgnoTeamPlanProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_parses_model_dict_and_json_and_prompt_contains_required_rules(self) -> None:
        catalog_snapshot = build_catalog_snapshot()
        context = _context()
        prompt_log: list[str] = []
        kwargs_log: list[dict[str, object]] = []

        cases = [
            ("model", _valid_plan()),
            ("dict", _valid_plan().model_dump(mode="json")),
            ("json", _valid_plan().model_dump_json()),
        ]
        for label, response in cases:
            with self.subTest(label=label):
                prompt_log.clear()
                kwargs_log.clear()

                def agent_factory(**kwargs: object) -> _FakeAgent:
                    kwargs_log.append(dict(kwargs))
                    return _FakeAgent(response, prompt_log, kwargs_log)

                provider = AgnoTeamPlanProvider(model=object(), agent_factory=agent_factory)
                plan = await provider.propose(
                    context,
                    catalog_snapshot,
                    ["missing_required_tool"],
                    2,
                )

                self.assertIsInstance(plan, TeamCompositionPlan)
                self.assertTrue(prompt_log)
                prompt = prompt_log[0]
                self.assertIn("No minimum team size", prompt)
                self.assertIn("Prefer the smallest capable team", prompt)
                self.assertIn("Acceptance requirements", prompt)
                instructions = kwargs_log[0]["instructions"]
                self.assertIn("Independent validation rule", " ".join(instructions))
                self.assertIn("Work-graph dependency and conflict rule", " ".join(instructions))
                self.assertIn("Owner limits: no minimum team size", " ".join(instructions))
                self.assertEqual(kwargs_log[0]["output_schema"], TeamCompositionPlan)
                self.assertEqual(kwargs_log[0]["structured_outputs"], True)

    async def test_provider_raises_typed_error_for_malformed_payload(self) -> None:
        def agent_factory(**_kwargs: object) -> _FakeAgent:
            return _FakeAgent("not-json", [], [])

        provider = AgnoTeamPlanProvider(model=object(), agent_factory=agent_factory)

        with self.assertRaises(TeamCompositionProviderError) as exc:
            await provider.propose(_context(), build_catalog_snapshot(), [], 1)
        self.assertEqual(exc.exception.code, "provider_malformed_response")


class CompositionContextTests(unittest.TestCase):
    def test_mutable_defaults_are_isolated(self) -> None:
        first = CompositionContext(task_id="one", task_summary="one")
        second = CompositionContext(task_id="two", task_summary="two")
        first.required_capabilities.append("implementation")
        first.acceptance_requirements.append("regression_checks")
        first.unresolved_user_requirements.append("Need exact success rubric")
        first.limits.max_model_workers = 9

        self.assertEqual(second.required_capabilities, [])
        self.assertIsNone(second.available_tool_ids)
        self.assertIsNone(second.available_agent_template_ids)
        self.assertEqual(second.acceptance_requirements, [])
        self.assertEqual(second.unresolved_user_requirements, [])
        self.assertEqual(second.limits.max_model_workers, CompositionLimits().max_model_workers)

    def test_explicit_empty_allowlists_remain_distinct_from_unspecified(self) -> None:
        unspecified = CompositionContext(task_id="one", task_summary="one")
        explicit_empty = CompositionContext(
            task_id="two",
            task_summary="two",
            available_tool_ids=[],
            available_agent_template_ids=[],
        )

        self.assertIsNone(unspecified.available_tool_ids)
        self.assertIsNone(unspecified.available_agent_template_ids)
        self.assertEqual(explicit_empty.available_tool_ids, [])
        self.assertEqual(explicit_empty.available_agent_template_ids, [])


def _context(
    *,
    task_id: str = "task-123",
    task_summary: str = "Implement the requested backend change.",
    user_request: str = "Please implement the team composer.",
    unresolved_user_requirements: list[str] | None = None,
) -> CompositionContext:
    return CompositionContext(
        task_id=task_id,
        task_summary=task_summary,
        user_request=user_request,
        required_capabilities=["implementation", "validation"],
        available_tool_ids=[
            "repository_write",
            "start_execution_environment",
            "execute_command",
            "export_artifact",
            "close_execution_environment",
            "read_artifacts",
            "rubric_check",
            "generate_image",
            "inspect_image",
            "publish_image",
        ],
        acceptance_requirements=["artifact_diff_review", "regression_checks"],
        unresolved_user_requirements=unresolved_user_requirements or [],
    )


if __name__ == "__main__":
    unittest.main()
