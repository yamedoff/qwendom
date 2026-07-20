from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.composition_runtime import CompositionExecutionResult, MaterializedAgentSummary, RuntimeAvailability
from society.memory import EventStore
from society.models import SocietyEvent
from society.orchestrator import SocietyOrchestrator
from society.schemas.team_composition import PlanValidationIssue, TeamAssignment, TeamCompositionPlan, ToolGrant, WorkNode
from society.specialist_selection import (
    ListSpecialistsResult,
    SelectSpecialistsCall,
    SpecialistAssignmentSelection,
)
from society.team_composer import CompositionContext, TeamCompositionBlocked, TeamCompositionResult
from society.work_graph import (
    WorkGraphExecutionResult,
    WorkGraphTerminalStatus,
    WorkNodeAttemptRecord,
    WorkNodeRuntimeRecord,
    WorkNodeStatus,
)


def _settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "qwen_legacy",
        "QWEN_LEGACY_API_KEY": "test-key",
        "QWEN_LEGACY_MODEL": "test-model",
        "TEAM_COMPOSITION_EXECUTION_ENABLED": True,
        "TEAM_COMPOSITION_STRATEGY": "legacy_composer",
        "EFFICIENT_SOCIETY_ENABLED": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_orchestrator(
    *,
    settings: Settings | None = None,
    event_store: EventStore | None = None,
) -> SocietyOrchestrator:
    settings = settings or _settings()
    with patch("society.orchestrator.get_agno_db", return_value=None):
        with patch("society.orchestrator.get_settings", return_value=settings):
            orchestrator = SocietyOrchestrator(settings=settings)
    if event_store is not None:
        orchestrator.events = event_store
        orchestrator._restore_from_events()
    return orchestrator


def _plan() -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Implement the accepted narrow integration.",
        assignments=[
            TeamAssignment(
                id="builder-assignment",
                agent_template_id="builder",
                objective="Implement the narrow backend change",
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
                owned_paths=["backend/society/orchestrator.py"],
                expected_artifacts=["patch"],
            ),
            TeamAssignment(
                id="critic-assignment",
                agent_template_id="critic",
                objective="Validate the implementation independently",
                required_capabilities=["validation"],
                tool_grants=[ToolGrant(capability="validation", tool_ids=["read_artifacts", "rubric_check"])],
                acceptance_checks=["artifact_diff_review"],
                validates_assignment_ids=["builder-assignment"],
            ),
        ],
        work_graph=[
            WorkNode(id="build-node", assignment_id="builder-assignment"),
            WorkNode(id="critic-node", assignment_id="critic-assignment", depends_on=["build-node"]),
        ],
        selection_rationale="One execution specialist and one independent validator are sufficient.",
    )


class _FakeComposer:
    def __init__(self, outcome: TeamCompositionResult | Exception, event_sink=None) -> None:
        self.outcome = outcome
        self.contexts: list[CompositionContext] = []
        self.event_sink = event_sink

    async def compose(self, context: CompositionContext) -> TeamCompositionResult:
        self.contexts.append(context)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.event_sink is not None:
            self.event_sink("team_composition_proposed", {"task_id": context.task_id, "attempt": 1})
            self.event_sink("team_composition_validated", {"task_id": context.task_id, "attempt": self.outcome.attempt_count})
        return self.outcome


class _FakeRuntime:
    def __init__(
        self,
        *,
        execution_result: CompositionExecutionResult | None = None,
        blockers: list[dict[str, str]] | None = None,
        event_sink=None,
    ) -> None:
        self.execution_result = execution_result or _execution_result()
        self.blockers = blockers or []
        self.execute_calls = 0
        self.event_sink = event_sink

    def available_tool_ids(self) -> RuntimeAvailability:
        return RuntimeAvailability(tool_ids=["start_execution_environment", "execute_command", "export_artifact"], blockers=self.blockers)

    def build_composition_context(
        self,
        task_id: str,
        user_request: str,
        acceptance_requirements: list[str],
        *,
        required_capabilities: list[str] = (),
        unresolved_user_requirements: list[str] = (),
    ) -> CompositionContext:
        return CompositionContext(
            task_id=task_id,
            task_summary=user_request,
            user_request=user_request,
            acceptance_requirements=list(acceptance_requirements),
            required_capabilities=list(required_capabilities),
            unresolved_user_requirements=list(unresolved_user_requirements),
            available_tool_ids=["start_execution_environment", "execute_command", "export_artifact"],
        )

    async def execute_plan(
        self,
        plan: TeamCompositionPlan,
        task_id: str,
        user_request: str,
        *,
        session_state: dict[str, object] | None = None,
        global_cancellation_event=None,
    ) -> CompositionExecutionResult:
        del plan, task_id, user_request, session_state, global_cancellation_event
        self.execute_calls += 1
        if self.event_sink is not None:
            for agent in self.execution_result.materialized_agents:
                self.event_sink(
                    "composition_assignment_materialized",
                    agent.model_dump(mode="json"),
                )
            for node_id, record in self.execution_result.graph_result.nodes.items():
                event_type = "work_node_completed" if record.status == WorkNodeStatus.COMPLETED else "work_node_failed"
                self.event_sink(
                    event_type,
                    {
                        "node_id": node_id,
                        "assignment_id": record.assignment_id,
                        "status": str(record.status),
                    },
                )
        return self.execution_result


def _execution_result(terminal_status: WorkGraphTerminalStatus = WorkGraphTerminalStatus.COMPLETED) -> CompositionExecutionResult:
    return CompositionExecutionResult(
        graph_result=WorkGraphExecutionResult(
            terminal_status=terminal_status,
            started_at=1.0,
            finished_at=2.0,
            duration_seconds=1.0,
            execution_order=["build-node", "critic-node"],
            max_model_concurrency=1,
            max_agentbay_concurrency=1,
            max_media_concurrency=0,
            nodes={
                "build-node": WorkNodeRuntimeRecord(
                    node_id="build-node",
                    assignment_id="builder-assignment",
                    status=WorkNodeStatus.COMPLETED if terminal_status == WorkGraphTerminalStatus.COMPLETED else WorkNodeStatus.FAILED,
                    attempts=[
                        WorkNodeAttemptRecord(
                            attempt=1,
                            status=WorkNodeStatus.COMPLETED if terminal_status == WorkGraphTerminalStatus.COMPLETED else WorkNodeStatus.FAILED,
                            category=None if terminal_status == WorkGraphTerminalStatus.COMPLETED else "capability",
                            code=None if terminal_status == WorkGraphTerminalStatus.COMPLETED else "missing_tool",
                            message=None if terminal_status == WorkGraphTerminalStatus.COMPLETED else "Granted tool is unavailable.",
                            started_at=1.0,
                            finished_at=1.5,
                            duration_seconds=0.5,
                        )
                    ],
                    result={"artifact_refs": ["artifact-build"], "summary": "implemented"} if terminal_status == WorkGraphTerminalStatus.COMPLETED else {},
                    artifact_refs=["artifact-build"] if terminal_status == WorkGraphTerminalStatus.COMPLETED else [],
                ),
                "critic-node": WorkNodeRuntimeRecord(
                    node_id="critic-node",
                    assignment_id="critic-assignment",
                    status=WorkNodeStatus.COMPLETED if terminal_status == WorkGraphTerminalStatus.COMPLETED else WorkNodeStatus.BLOCKED,
                    blocked_dependency_ids=[] if terminal_status == WorkGraphTerminalStatus.COMPLETED else ["build-node"],
                    attempts=[
                        WorkNodeAttemptRecord(
                            attempt=1,
                            status=WorkNodeStatus.COMPLETED if terminal_status == WorkGraphTerminalStatus.COMPLETED else WorkNodeStatus.BLOCKED,
                            category=None,
                            code=None,
                            message=None if terminal_status == WorkGraphTerminalStatus.COMPLETED else "Dependency build-node failed.",
                            started_at=1.5,
                            finished_at=2.0,
                            duration_seconds=0.5,
                        )
                    ] if terminal_status == WorkGraphTerminalStatus.COMPLETED else [],
                    result={"artifact_refs": ["artifact-critic"], "summary": "validated"} if terminal_status == WorkGraphTerminalStatus.COMPLETED else {},
                    artifact_refs=["artifact-critic"] if terminal_status == WorkGraphTerminalStatus.COMPLETED else [],
                ),
            },
        ),
        materialized_agents=[
            MaterializedAgentSummary(
                id="assignment-builder-assignment",
                assignment_id="builder-assignment",
                template_id="builder",
                capabilities=["implementation", "sandbox_execution"],
                can_vote=False,
            ),
            MaterializedAgentSummary(
                id="assignment-critic-assignment",
                assignment_id="critic-assignment",
                template_id="critic",
                capabilities=["validation"],
                can_vote=False,
            ),
        ],
        node_outputs={
            "build-node": {"summary": "implemented"},
            "critic-node": {"summary": "validated"},
        } if terminal_status == WorkGraphTerminalStatus.COMPLETED else {},
        node_artifact_refs={
            "build-node": ["artifact-build"],
            "critic-node": ["artifact-critic"],
        } if terminal_status == WorkGraphTerminalStatus.COMPLETED else {},
        availability_blockers=[],
    )


def _canceled_execution_result() -> CompositionExecutionResult:
    return CompositionExecutionResult(
        graph_result=WorkGraphExecutionResult(
            terminal_status=WorkGraphTerminalStatus.CANCELED,
            started_at=1.0,
            finished_at=2.0,
            duration_seconds=1.0,
            execution_order=["build-node"],
            max_model_concurrency=1,
            max_agentbay_concurrency=1,
            max_media_concurrency=0,
            nodes={
                "build-node": WorkNodeRuntimeRecord(
                    node_id="build-node",
                    assignment_id="builder-assignment",
                    status=WorkNodeStatus.CANCELED,
                    attempts=[],
                    result={},
                    artifact_refs=[],
                ),
            },
        ),
        materialized_agents=[
            MaterializedAgentSummary(
                id="assignment-builder-assignment",
                assignment_id="builder-assignment",
                template_id="builder",
                capabilities=["implementation", "sandbox_execution"],
                can_vote=False,
            ),
        ],
        node_outputs={},
        node_artifact_refs={},
        availability_blockers=[],
    )


class _BlockingShutdownRuntime(_FakeRuntime):
    def __init__(self, *, event_sink=None) -> None:
        super().__init__(execution_result=_canceled_execution_result(), event_sink=event_sink)
        self.started = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.cancel_seen = asyncio.Event()

    async def execute_plan(
        self,
        plan: TeamCompositionPlan,
        task_id: str,
        user_request: str,
        *,
        session_state: dict[str, object] | None = None,
        global_cancellation_event=None,
    ) -> CompositionExecutionResult:
        del plan, task_id, user_request, session_state
        self.execute_calls += 1
        self.started.set()
        assert global_cancellation_event is not None
        await asyncio.wait_for(global_cancellation_event.wait(), timeout=0.5)
        self.cancel_seen.set()
        self.cleanup_finished.set()
        if self.event_sink is not None:
            self.event_sink(
                "work_node_canceled",
                {
                    "node_id": "build-node",
                    "assignment_id": "builder-assignment",
                    "status": str(WorkNodeStatus.CANCELED),
                },
            )
        return self.execution_result


def _task_and_team(orch: SocietyOrchestrator) -> tuple[object, object]:
    task = orch.submit("Implement the narrow accepted integration with independent validation.")
    team = orch._form_team(task)
    team.leader_id = "architect"
    orch._state(task.id)["working_brief"] = {
        "summary": task.prompt,
        "agreed_scope": task.prompt,
        "success_criteria": ["Ship the integration", "Preserve truthful failures"],
        "constraints": [],
        "open_questions": [],
        "blocked_items": [],
        "unresolved_dissent": [],
        "assumptions": [],
        "question_answers": [],
        "confidence": 0.8,
    }
    orch._state(task.id)["team_coordination_brief"] = {
        "summary": "Coordinate implementation and independent validation.",
        "dependencies": ["Validation depends on the implementation artifact."],
        "proposed_subtasks": [
            {
                "agent_id": "builder",
                "subtask": "Implement the requested narrow backend change.",
                "required_capabilities": ["implementation", "sandbox_execution"],
            },
            {
                "agent_id": "critic",
                "subtask": "Independently validate the implementation artifact.",
                "required_capabilities": ["validation"],
            },
        ],
        "open_questions": ["Confirm the exact acceptance checks to run."],
    }
    orch._state(task.id)["readiness_tally"] = {
        "open_questions": ["Need the user to clarify the acceptance checks."],
        "blockers": [],
    }
    # Legacy-composer fixtures model durable historical resumes. New-product
    # routing is fixed-specialist even when the environment requests legacy.
    if orch.settings.team_composition_strategy == "legacy_composer":
        orch._state(task.id).update({
            "resume_phase": "team_composition",
            "team_composition": {"selection_strategy": "legacy_composer"},
        })
    return task, team


class OrchestratorTeamCompositionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_strategy_uses_elected_leader_and_persists_immutable_selection(self) -> None:
        orch = _make_orchestrator(settings=_settings(TEAM_COMPOSITION_STRATEGY="fixed_specialists"))
        task, team = _task_and_team(orch)

        class FixedRuntime(_FakeRuntime):
            def available_tool_ids(self) -> RuntimeAvailability:
                from society.capability_registry import list_fixed_specialist_templates

                return RuntimeAvailability(tool_ids=sorted({
                    tool_id
                    for template in list_fixed_specialist_templates().values()
                    for tool_id in template.tool_ids
                }))

        runtime = FixedRuntime()

        async def governance_call(
            _task,
            actor,
            _tool_func,
            tool_name,
            _schema,
            _prompt,
            _extra=None,
        ):
            self.assertEqual(actor.id, "architect")
            if tool_name == "list_specialists":
                return ListSpecialistsResult.model_validate({
                    "specialists": orch._state(task.id)["fixed_specialist_catalog"]
                })
            self.assertEqual(tool_name, "select_specialists")
            return SelectSpecialistsCall(
                selection_rationale="Builder implementation followed by independent validation.",
                assignments=[
                    SpecialistAssignmentSelection(
                        assignment_id="build",
                        template_id="builder",
                        objective="Implement the bounded repair.",
                        depends_on=[],
                        owned_artifacts=["src/calc.py"],
                        acceptance_requirements=["unit_tests"],
                    ),
                    SpecialistAssignmentSelection(
                        assignment_id="validate",
                        template_id="test_engineer",
                        objective="Validate the exported repair.",
                        depends_on=["build"],
                        owned_artifacts=[],
                        acceptance_requirements=["unit_tests"],
                    ),
                ],
            )

        orch._run_governance_tool = AsyncMock(side_effect=governance_call)
        context = runtime.build_composition_context(task.id, task.prompt, ["unit_tests"])

        result = await orch._select_fixed_specialist_plan(task, team, context, runtime)

        self.assertEqual([item.agent_template_id for item in result.plan.assignments], ["builder", "test_engineer"])
        self.assertEqual(result.plan.work_graph[1].depends_on, ["build"])
        self.assertEqual(result.plan.assignments[0].resolved_skills[0].skill_id, "repository_implementation")
        self.assertEqual(result.plan.assignments[1].resolved_skills[0].skill_id, "independent_validation")
        self.assertEqual(orch._state(task.id)["fixed_specialist_selection"]["strategy"], "fixed_specialists")
        self.assertEqual(
            [call.args[3] for call in orch._run_governance_tool.await_args_list],
            ["list_specialists", "select_specialists"],
        )

    async def test_hallucinated_catalog_cannot_expand_fixed_specialist_selection(self) -> None:
        orch = _make_orchestrator(settings=_settings(TEAM_COMPOSITION_STRATEGY="fixed_specialists"))
        task, team = _task_and_team(orch)

        class FixedRuntime(_FakeRuntime):
            def available_tool_ids(self) -> RuntimeAvailability:
                from society.capability_registry import list_fixed_specialist_templates

                return RuntimeAvailability(tool_ids=sorted({
                    tool_id
                    for template in list_fixed_specialist_templates().values()
                    for tool_id in template.tool_ids
                }))

        prompts: list[str] = []

        async def governance_call(_task, _actor, _tool_func, tool_name, _schema, prompt, _extra=None):
            if tool_name == "list_specialists":
                invented = dict(orch._state(task.id)["fixed_specialist_catalog"][0])
                invented["template_id"] = "quality_gate_engineer"
                return ListSpecialistsResult.model_validate({"specialists": [invented]})
            self.assertEqual(tool_name, "select_specialists")
            prompts.append(prompt)
            if len(prompts) == 1:
                return SelectSpecialistsCall(
                    selection_rationale="Use the invented quality gate.",
                    assignments=[SpecialistAssignmentSelection(
                        assignment_id="quality", template_id="quality_gate_engineer",
                        objective="Validate the code.", depends_on=[], owned_artifacts=[], acceptance_requirements=[],
                    )],
                )
            return SelectSpecialistsCall(
                selection_rationale="Build then independently validate the code artifact.",
                assignments=[
                    SpecialistAssignmentSelection(
                        assignment_id="build", template_id="builder", objective="Implement the code artifact.",
                        depends_on=[], owned_artifacts=["src/calc.py"], acceptance_requirements=["unit_tests"],
                    ),
                    SpecialistAssignmentSelection(
                        assignment_id="validate", template_id="test_engineer", objective="Independently validate it.",
                        depends_on=["build"], owned_artifacts=[], acceptance_requirements=["unit_tests"],
                    ),
                ],
            )

        orch._run_governance_tool = AsyncMock(side_effect=governance_call)
        runtime = FixedRuntime()
        context = runtime.build_composition_context(task.id, task.prompt, ["unit_tests"])

        result = await orch._select_fixed_specialist_plan(task, team, context, runtime)

        self.assertEqual([item.agent_template_id for item in result.plan.assignments], ["builder", "test_engineer"])
        self.assertEqual(result.plan.work_graph[1].depends_on, ["build"])
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("quality_gate_engineer", prompts[0])
        self.assertIn('Valid template IDs are exactly:', prompts[1])
        self.assertIn('"builder"', prompts[1])
        self.assertIn('"test_engineer"', prompts[1])

    async def test_disabled_path_preserves_legacy_delegation_seam(self) -> None:
        orch = _make_orchestrator(settings=_settings(TEAM_COMPOSITION_EXECUTION_ENABLED=False))
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()

        # A proved historical legacy resume remains available; the disabled
        # flag cannot turn it into a new-run fallback.
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_system_capability",
                message="Legacy replay prerequisites are unavailable.",
                issues=[PlanValidationIssue(code="legacy_unavailable", message="Legacy replay prerequisites are unavailable.")],
            )
        )
        await orch._execute_after_readiness(task, team, 1.0)

        orch._delegate_subtasks.assert_not_awaited()
        orch._continue_after_execution.assert_not_awaited()
        self.assertEqual(task.status, "failed")

    async def test_disabled_fixed_specialist_path_fails_instead_of_using_legacy_delegation(self) -> None:
        orch = _make_orchestrator(settings=_settings(
            TEAM_COMPOSITION_EXECUTION_ENABLED=False,
            TEAM_COMPOSITION_STRATEGY="fixed_specialists",
        ))
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        orch._select_fixed_specialist_plan = AsyncMock(side_effect=TeamCompositionBlocked(
            category="missing_system_capability",
            message="AgentBay is unavailable.",
            issues=[PlanValidationIssue(code="agentbay_unavailable", message="AgentBay is unavailable.")],
        ))

        await orch._execute_after_readiness(task, team, 1.0)

        self.assertEqual(task.status, "failed")
        orch._delegate_subtasks.assert_not_awaited()
        orch._continue_after_execution.assert_not_awaited()
        self.assertEqual(orch._team_composition_state(task.id)["status"], "failed")
        failure = next(event for event in orch.list_events(task.id) if event.type == "task_failed")
        self.assertEqual(failure.payload["blocker_category"], "missing_system_capability")

    async def test_legacy_environment_strategy_cannot_route_a_new_mission_to_legacy_composer(self) -> None:
        orch = _make_orchestrator(settings=_settings(TEAM_COMPOSITION_STRATEGY="legacy_composer"))
        task, team = _task_and_team(orch)
        orch._state(task.id).pop("resume_phase", None)
        orch._state(task.id).pop("team_composition", None)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        orch._create_team_composer = lambda _task_id: self.fail("new runs must not instantiate TeamComposer")
        orch._select_fixed_specialist_plan = AsyncMock(side_effect=TeamCompositionBlocked(
            category="missing_system_capability",
            message="Fixed specialist prerequisites are unavailable.",
            issues=[PlanValidationIssue(code="agentbay_unavailable", message="AgentBay is unavailable.")],
        ))

        await orch._execute_after_readiness(task, team, 1.0)

        self.assertEqual(task.status, "failed")
        orch._select_fixed_specialist_plan.assert_awaited_once()
        orch._delegate_subtasks.assert_not_awaited()

    async def test_successful_composed_execution_persists_state_and_skips_legacy_delegation(self) -> None:
        orch = _make_orchestrator()
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()

        composer = _FakeComposer(
            TeamCompositionResult(
                plan=_plan(),
                attempt_count=2,
                recomposed=True,
                validation_issue_history=[[PlanValidationIssue(code="missing_required_tool", message="Need export_artifact")]],
            )
        )
        runtime = _FakeRuntime()
        orch._create_team_composer = lambda _task_id: composer
        orch._create_composition_runtime = lambda _task_id: runtime

        await orch._execute_after_readiness(task, team, 1.0)

        orch._delegate_subtasks.assert_not_awaited()
        orch._continue_after_execution.assert_awaited_once()
        state = orch._state(task.id)
        composition = state["team_composition"]
        self.assertEqual(composition["status"], "completed")
        self.assertEqual(composition["attempt_count"], 2)
        self.assertTrue(composition["recomposed"])
        self.assertEqual(len(state["composition_execution_roster"]), 2)
        self.assertTrue(all(agent["can_vote"] is False for agent in state["composition_execution_roster"]))
        self.assertEqual(team.voter_ids, ["architect", "researcher", "builder", "critic"])
        self.assertEqual(len(state["subtasks"]), 2)
        self.assertEqual(len({subtask["id"] for subtask in state["subtasks"]}), 2)
        self.assertEqual([subtask["id"] for subtask in state["subtasks"]], ["build-node", "critic-node"])
        self.assertNotIn("assignment-builder-assignment", team.voter_ids)

    async def test_missing_user_input_pauses_without_fallback(self) -> None:
        orch = _make_orchestrator()
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_user_input",
                message="Need acceptance criteria clarified.",
                issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need acceptance criteria clarified.")],
            )
        )
        orch._create_composition_runtime = lambda _task_id: _FakeRuntime()

        await orch._execute_after_readiness(task, team, 1.0)

        self.assertEqual(task.status, "waiting_for_user")
        self.assertEqual(orch._state(task.id)["resume_phase"], "team_composition")
        orch._delegate_subtasks.assert_not_awaited()
        orch._continue_after_execution.assert_not_awaited()
        last_event = orch.list_events(task.id)[-1]
        self.assertEqual(last_event.type, "user_clarification_requested")
        self.assertEqual(last_event.payload["resume_phase"], "team_composition")

    async def test_resume_from_team_composition_does_not_rerun_precomposition_phases(self) -> None:
        orch = _make_orchestrator()
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_user_input",
                message="Need acceptance criteria clarified.",
                issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need acceptance criteria clarified.")],
            )
        )
        orch._create_composition_runtime = lambda _task_id: _FakeRuntime()

        await orch._execute_after_readiness(task, team, 1.0)

        orch._run_agno_team.reset_mock()
        orch._elect_leader.reset_mock()
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionResult(plan=_plan(), attempt_count=1, recomposed=False)
        )
        orch._create_composition_runtime = lambda _task_id: _FakeRuntime()

        orch.apply_user_clarification(task.id, "Use artifact_diff_review as the acceptance check.")
        await orch.continue_after_clarification(task.id)

        orch._run_agno_team.assert_not_awaited()
        orch._elect_leader.assert_not_awaited()
        orch._continue_after_execution.assert_awaited_once()
        self.assertTrue(orch._team_composition_state(task.id)["resume_used"])

    async def test_restart_reconstructs_team_composition_pause_and_resumes_once(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = EventStore(temp_dir / "events.jsonl")
        orch1 = _make_orchestrator(event_store=store)
        task, team = _task_and_team(orch1)
        orch1._run_agno_team = AsyncMock()
        orch1._elect_leader = AsyncMock()
        orch1._delegate_subtasks = AsyncMock()
        orch1._continue_after_execution = AsyncMock()
        orch1._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_user_input",
                message="Need acceptance criteria clarified.",
                issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need acceptance criteria clarified.")],
            )
        )
        orch1._create_composition_runtime = lambda _task_id: _FakeRuntime()

        await orch1._execute_after_readiness(task, team, 1.0)

        orch2 = _make_orchestrator(event_store=store)
        self.assertIn(task.id, orch2.tasks)
        self.assertEqual(orch2.tasks[task.id].status, "waiting_for_user")
        self.assertEqual(orch2._state(task.id)["resume_phase"], "team_composition")
        self.assertEqual(orch2._state(task.id)["team_composition"]["status"], "waiting_for_user")

        orch2._run_agno_team = AsyncMock()
        orch2._elect_leader = AsyncMock()
        orch2._continue_after_execution = AsyncMock()
        orch2._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionResult(plan=_plan(), attempt_count=1, recomposed=False)
        )
        orch2._create_composition_runtime = lambda _task_id: _FakeRuntime()

        orch2.apply_user_clarification(task.id, "Use artifact_diff_review as the acceptance check.")
        await orch2.continue_after_clarification(task.id)

        orch2._run_agno_team.assert_not_awaited()
        orch2._elect_leader.assert_not_awaited()
        orch2._continue_after_execution.assert_awaited_once()
        self.assertTrue(orch2._team_composition_state(task.id)["resume_used"])

    async def test_restart_prefers_pause_payload_governance_roster_voters_and_leader(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = EventStore(temp_dir / "events.jsonl")
        orch1 = _make_orchestrator(event_store=store)
        task, team = _task_and_team(orch1)
        orch1._run_agno_team = AsyncMock()
        orch1._elect_leader = AsyncMock()
        orch1._delegate_subtasks = AsyncMock()
        orch1._continue_after_execution = AsyncMock()
        orch1._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_user_input",
                message="Need acceptance criteria clarified.",
                issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need acceptance criteria clarified.")],
            )
        )
        orch1._create_composition_runtime = lambda _task_id: _FakeRuntime()

        await orch1._execute_after_readiness(task, team, 1.0)

        pause_payload = dict(orch1.list_events(task.id)[-1].payload)
        pause_payload.update({
            "team_id": team.id,
            "team_member_ids": ["architect", "researcher", "builder", "critic"],
            "voter_ids": ["researcher", "builder"],
            "team_leader_id": "researcher",
        })
        store.append(SocietyEvent(task_id=task.id, type="user_clarification_requested", message="override", payload=pause_payload))

        orch2 = _make_orchestrator(event_store=store)
        restored_team = orch2.teams[orch2.tasks[task.id].team_id]
        self.assertEqual(restored_team.member_ids, ["architect", "researcher", "builder", "critic"])
        self.assertEqual(restored_team.voter_ids, ["researcher", "builder"])
        self.assertEqual(restored_team.leader_id, "researcher")
        self.assertNotIn("assignment-builder-assignment", restored_team.voter_ids)

    async def test_restart_resume_uses_persisted_context_snapshot_and_honors_flag_flip(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = EventStore(temp_dir / "events.jsonl")
        orch1 = _make_orchestrator(settings=_settings(TEAM_COMPOSITION_EXECUTION_ENABLED=True), event_store=store)
        task, team = _task_and_team(orch1)
        orch1._run_agno_team = AsyncMock()
        orch1._elect_leader = AsyncMock()
        orch1._delegate_subtasks = AsyncMock()
        orch1._continue_after_execution = AsyncMock()
        orch1._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_user_input",
                message="Need acceptance criteria clarified.",
                issues=[PlanValidationIssue(code="missing_acceptance_checks", message="Need acceptance criteria clarified.")],
            )
        )
        orch1._create_composition_runtime = lambda _task_id: _FakeRuntime()

        await orch1._execute_after_readiness(task, team, 1.0)

        snapshot = orch1._team_composition_state(task.id)["context_snapshot"]
        self.assertIsInstance(snapshot, dict)

        orch2 = _make_orchestrator(
            settings=_settings(TEAM_COMPOSITION_EXECUTION_ENABLED=False),
            event_store=store,
        )
        orch2._run_agno_team = AsyncMock()
        orch2._elect_leader = AsyncMock()
        orch2._delegate_subtasks = AsyncMock()
        orch2._continue_after_execution = AsyncMock()
        resumed_composer = _FakeComposer(TeamCompositionResult(plan=_plan(), attempt_count=1, recomposed=False))
        orch2._create_team_composer = lambda _task_id: resumed_composer
        orch2._create_composition_runtime = lambda _task_id: _FakeRuntime()

        clarification = "Use artifact_diff_review and regression_checks as the acceptance checks."
        orch2.apply_user_clarification(task.id, clarification)
        await orch2.continue_after_clarification(task.id)

        orch2._run_agno_team.assert_not_awaited()
        orch2._elect_leader.assert_not_awaited()
        orch2._delegate_subtasks.assert_not_awaited()
        orch2._continue_after_execution.assert_awaited_once()
        self.assertEqual(len(resumed_composer.contexts), 1)
        resumed_context = resumed_composer.contexts[0]
        self.assertEqual(resumed_context.required_capabilities, snapshot["required_capabilities"])
        self.assertEqual(resumed_context.acceptance_requirements, snapshot["acceptance_requirements"])
        self.assertEqual(
            resumed_context.unresolved_user_requirements[:-1],
            snapshot["unresolved_user_requirements"],
        )
        self.assertEqual(
            resumed_context.unresolved_user_requirements[-1],
            f"User clarification: {clarification}",
        )
        self.assertEqual(orch2._team_composition_state(task.id)["status"], "completed")

    async def test_system_capability_block_fails_truthfully_and_does_not_fallback(self) -> None:
        orch = _make_orchestrator()
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionBlocked(
                category="missing_system_capability",
                message="Builder execution tools are unavailable.",
                issues=[PlanValidationIssue(code="missing_required_tool", message="Builder execution tools are unavailable.")],
            )
        )
        orch._create_composition_runtime = lambda _task_id: _FakeRuntime()

        await orch._execute_after_readiness(task, team, 1.0)

        self.assertEqual(task.status, "failed")
        orch._delegate_subtasks.assert_not_awaited()
        orch._continue_after_execution.assert_not_awaited()
        event_types = [event.type for event in orch.list_events(task.id)]
        self.assertIn("run_failed", event_types)
        self.assertIn("task_failed", event_types)

    async def test_composition_events_and_state_are_json_serializable(self) -> None:
        orch = _make_orchestrator()
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        event_sink = lambda event_type, payload: orch._emit_team_composition_event(task.id, event_type, dict(payload))
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionResult(plan=_plan(), attempt_count=1, recomposed=False),
            event_sink=event_sink,
        )
        orch._create_composition_runtime = lambda _task_id: _FakeRuntime(event_sink=event_sink)

        await orch._execute_after_readiness(task, team, 1.0)

        serialized_events = json.dumps([event.model_dump(mode="json") for event in orch.list_events(task.id)])
        serialized_state = json.dumps(orch._state(task.id))
        self.assertIn("team_composition_proposed", serialized_events)
        self.assertIn("work_node_completed", serialized_events)
        self.assertIn("delegation_reported", serialized_events)
        self.assertIn("execution_result", serialized_state)

    async def test_shutdown_cancels_active_composition_execution_and_waits_for_runtime_exit(self) -> None:
        orch = _make_orchestrator()
        task, team = _task_and_team(orch)
        orch._run_agno_team = AsyncMock()
        orch._elect_leader = AsyncMock()
        orch._delegate_subtasks = AsyncMock()
        orch._continue_after_execution = AsyncMock()
        event_sink = lambda event_type, payload: orch._emit_team_composition_event(task.id, event_type, dict(payload))
        orch._create_team_composer = lambda _task_id: _FakeComposer(
            TeamCompositionResult(plan=_plan(), attempt_count=1, recomposed=False)
        )
        runtime = _BlockingShutdownRuntime(event_sink=event_sink)
        orch._create_composition_runtime = lambda _task_id: runtime

        execution_task = asyncio.create_task(orch._execute_after_readiness(task, team, 1.0))
        await asyncio.wait_for(runtime.started.wait(), timeout=0.5)

        orch.shutdown()

        await asyncio.wait_for(execution_task, timeout=0.5)

        self.assertTrue(runtime.cancel_seen.is_set())
        self.assertTrue(runtime.cleanup_finished.is_set())
        self.assertNotIn(task.id, orch._composition_cancellation_events)
        self.assertEqual(task.status, "interrupted")
        self.assertEqual(orch._team_composition_state(task.id)["status"], "canceled")
        self.assertEqual(
            orch._team_composition_state(task.id)["execution_result"]["graph_result"]["terminal_status"],
            "canceled",
        )
        self.assertIn("task_interrupted", [event.type for event in orch.list_events(task.id)])
        self.assertIn("work_node_canceled", [event.type for event in orch.list_events(task.id)])
        orch._continue_after_execution.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
