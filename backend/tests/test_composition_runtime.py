"""Focused tests for the credential-aware Phase 4 composition runtime."""

from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.composition_runtime import (
    AGENTBAY_RUNTIME_TOOL_IDS,
    IMAGE_RUNTIME_TOOL_IDS,
    LOCAL_ARTIFACT_TOOL_IDS,
    VIDEO_RUNTIME_TOOL_IDS,
    AgnoAssignmentExecutor,
    CompositionRuntime,
    _tool_call_limit_for_assignment,
)
from society.schemas.team_composition import TeamAssignment, TeamCompositionPlan, ToolGrant, WorkNode
from society.work_graph import NodeExecutionError, WorkGraphTerminalStatus, WorkNodeStatus


def _settings() -> Settings:
    return Settings(
        LLM_PROVIDER="qwen",
        QWEN_API_KEY="sk-test",
        QWEN_MODEL="qwen3.7-plus",
        DASHSCOPE_API_KEY="dashscope",
        AGENTBAY_API_KEY="agentbay",
        MODEL_STUDIO_WORKSPACE_ID="workspace",
        ROLE_SPECIFIC_TOOLS_ENABLED=True,
        CONTEXT7_MCP_ENABLED=True,
    )


def _preflight(*, agentbay: bool = True, browser: bool = True, image: bool = True, video: bool = True, blockers: list[dict[str, str]] | None = None):
    def inner(_settings: Settings) -> dict[str, Any]:
        return {
            "provider_services": {
                "agentbay": {"ready": agentbay},
                "browser": {"ready": browser},
                "image": {"ready": image},
                "video": {"ready": video},
            },
            "typed_blockers": blockers or [],
        }

    return inner


def _assignment(
    assignment_id: str,
    template_id: str,
    *,
    required_capabilities: list[str] | None = None,
    tool_ids: list[str] | None = None,
    owned_paths: list[str] | None = None,
) -> TeamAssignment:
    capabilities = required_capabilities or []
    return TeamAssignment(
        id=assignment_id,
        agent_template_id=template_id,
        objective=f"Run {assignment_id}",
        required_capabilities=capabilities,
        tool_grants=[ToolGrant(capability=capabilities[0], tool_ids=tool_ids or [])] if capabilities and tool_ids is not None else [],
        owned_paths=owned_paths or [],
        expected_artifacts=[f"{assignment_id}-artifact"],
    )


def _plan(assignments: list[TeamAssignment], nodes: list[WorkNode]) -> TeamCompositionPlan:
    return TeamCompositionPlan(
        task_summary="Runtime test plan",
        assignments=assignments,
        work_graph=nodes,
        selection_rationale="Deterministic runtime coverage.",
    )


class CompositionRuntimeAvailabilityTests(unittest.TestCase):
    def test_fixed_assignment_limits_keep_builder_and_validator_bounded(self) -> None:
        assignment = TeamAssignment(
            id="builder",
            agent_template_id="builder",
            objective="Repair and export every required artifact",
            expected_artifacts=[f"reports/artifact-{index}.json" for index in range(8)],
            template_version="1",
        )

        self.assertEqual(_tool_call_limit_for_assignment(assignment, 8), 18)
        assignment.agent_template_id = "test_engineer"
        self.assertEqual(_tool_call_limit_for_assignment(assignment, 8), 16)
        assignment.template_version = None
        self.assertEqual(_tool_call_limit_for_assignment(assignment, 8), 16)

    def test_availability_truth_matches_preflight_and_local_tools(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=True),
            )
            availability = runtime.available_tool_ids()
            expected = set(AGENTBAY_RUNTIME_TOOL_IDS) | set(VIDEO_RUNTIME_TOOL_IDS) | set(LOCAL_ARTIFACT_TOOL_IDS) | {"context7_lookup", "execute_notes_demo", "risk_assessment"}
            self.assertEqual(set(availability.tool_ids), expected)
            self.assertNotIn("repository_write", availability.tool_ids)
            self.assertNotIn("read_artifacts", availability.tool_ids)
            self.assertNotIn("generate_image", availability.tool_ids)

    def test_availability_blockers_are_typed_and_secret_safe(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(
                    agentbay=False,
                    image=False,
                    video=False,
                    blockers=[{
                        "code": "agentbay_api_key_missing",
                        "category": "missing_user_input",
                        "service": "agentbay",
                        "reason": "Missing AgentBay API key.",
                        "remediation": "Set AGENTBAY_API_KEY.",
                    }],
                ),
            )
            availability = runtime.available_tool_ids()
            self.assertEqual(len(availability.blockers), 1)
            self.assertEqual(availability.blockers[0].code, "agentbay_api_key_missing")
            self.assertNotIn("agentbay", "".join(tool for tool in availability.tool_ids if tool == "repository_write"))

    def test_build_context_uses_limits_truthful_tools_and_no_secrets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = CompositionRuntime(_settings(), temp_dir, preflight_fn=_preflight())
            context = runtime.build_composition_context(
                "task-1",
                "secret request sk-live-123",
                ["artifact_diff_review"],
                required_capabilities=["implementation"],
                unresolved_user_requirements=["Need output format"],
            )
            self.assertEqual(context.limits.max_model_workers, _settings().society_max_model_workers)
            self.assertIn("generate_images", context.available_tool_ids)
            self.assertNotIn("repository_write", context.available_tool_ids)
            self.assertEqual(context.unresolved_user_requirements, ["Need output format"])


class CompositionRuntimeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_contract_exports_missing_owned_artifact_through_agentbay(self) -> None:
        with TemporaryDirectory() as temp_dir:
            events: list[tuple[str, dict[str, Any]]] = []
            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                event_sink=lambda event_type, payload: events.append((event_type, dict(payload))),
                preflight_fn=_preflight(),
            )
            durable = Path(temp_dir) / "agentbay" / "contract_report.json"
            durable.parent.mkdir(parents=True)
            durable.write_text('{"passed": true}', encoding="utf-8")
            outside = Path(temp_dir) / "untrusted_report.json"
            outside.write_text('{"passed": false}', encoding="utf-8")
            calls: list[tuple[str, str, str]] = []

            class FakeToolkit:
                async def export_artifact(self, handle: str, path: str, artifact_kind: str) -> dict[str, Any]:
                    calls.append((handle, path, artifact_kind))
                    return {
                        "success": True,
                        "data": {"artifact": {"path": str(durable)}},
                    }

            assignment = TeamAssignment(
                id="builder",
                agent_template_id="builder",
                objective="Produce a deterministic test report.",
                expected_artifacts=["reports/test_report.json"],
                template_version="1",
            )
            result = await runtime._assignment_executor._enforce_required_artifact_exports(
                assignment=assignment,
                node=WorkNode(id="builder-node", assignment_id="builder"),
                result={
                    "workspace_exports": [{
                        "workspace_relative_path": "reports/test_report.json",
                        "artifact_ref": str(outside),
                    }]
                },
                internal_trace=[],
                agentbay_toolkit=FakeToolkit(),
                cached_start_result={"success": True, "data": {"handle": "opaque-handle"}},
            )

            self.assertEqual(calls, [("opaque-handle", "/workspace/reports/test_report.json", "report")])
            self.assertEqual(
                result["workspace_exports"],
                [{
                    "workspace_relative_path": "reports/test_report.json",
                    "artifact_ref": str(durable),
                }],
            )
            self.assertIn(
                "composition_required_artifacts_exported",
                [event_type for event_type, _payload in events],
            )

    def test_assignment_prompt_prioritizes_files_and_documents_exact_command_allowlist(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = CompositionRuntime(_settings(), temp_dir, preflight_fn=_preflight())
            assignment = TeamAssignment(
                id="builder",
                agent_template_id="builder",
                objective="Repair and report.",
                expected_artifacts=["repo/services/api/auth.py", "reports/test_report.json"],
                template_version="1",
            )
            prompt = runtime._assignment_executor._build_prompt(
                assignment=assignment,
                tool_ids=["execute_command", "run_code", "write_text_file", "export_artifact"],
                skill_instructions=["Verified skill."],
                direct_dependency_outputs={},
                user_request="Repair the fixture.",
            )

            self.assertIn("Create or update every expected artifact before optional exploration", prompt)
            self.assertIn("`pytest_target` and `python_compile`", prompt)
            self.assertIn("runtime verifies and finalizes durable exports", prompt)
            self.assertIn('arguments {"target": "/workspace/repo"}', prompt)

    async def test_ten_materialized_identities_are_distinct_nonvoting_and_do_not_mutate_registry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            assignments = [
                _assignment(f"node-{index}", "builder", required_capabilities=["implementation"], tool_ids=[])
                for index in range(10)
            ]
            plan = _plan(assignments, [WorkNode(id=item.id, assignment_id=item.id) for item in assignments])

            async def executor(**kwargs: Any) -> dict[str, Any]:
                return {"assignment_id": kwargs["assignment"].id}

            runtime = CompositionRuntime(_settings(), temp_dir, assignment_executor=executor, preflight_fn=_preflight())
            result = await runtime.execute_plan(plan, "task", "request")
            self.assertEqual(len(result.materialized_agents), 10)
            self.assertEqual(len({agent.id for agent in result.materialized_agents}), 10)
            self.assertTrue(all(agent.can_vote is False for agent in result.materialized_agents))

    async def test_dependency_outputs_are_direct_only_and_independent_work_overlaps(self) -> None:
        with TemporaryDirectory() as temp_dir:
            plan = _plan(
                [
                    _assignment("a", "builder", required_capabilities=["implementation"], tool_ids=[]),
                    _assignment("b", "builder", required_capabilities=["implementation"], tool_ids=[]),
                    _assignment("c", "builder", required_capabilities=["implementation"], tool_ids=[]),
                ],
                [
                    WorkNode(id="a", assignment_id="a"),
                    WorkNode(id="b", assignment_id="b"),
                    WorkNode(id="c", assignment_id="c", depends_on=["a"]),
                ],
            )
            running = 0
            peak = 0
            gate = asyncio.Event()
            overlap = asyncio.Event()
            captured: dict[str, dict[str, Any]] = {}
            lock = asyncio.Lock()

            async def executor(**kwargs: Any) -> dict[str, Any]:
                nonlocal running, peak
                async with lock:
                    running += 1
                    peak = max(peak, running)
                    if peak == 2:
                        overlap.set()
                assignment = kwargs["assignment"].id
                captured[assignment] = dict(kwargs["direct_dependency_outputs"])
                if assignment in {"a", "b"}:
                    await gate.wait()
                async with lock:
                    running -= 1
                return {"artifact_refs": [assignment], "blob": "x" * 1000}

            runtime = CompositionRuntime(_settings(), temp_dir, assignment_executor=executor, preflight_fn=_preflight())
            task = asyncio.create_task(runtime.execute_plan(plan, "task", "request"))
            await asyncio.wait_for(overlap.wait(), timeout=1.0)
            gate.set()
            result = await task
            self.assertEqual(peak, 2)
            self.assertEqual(captured["a"], {})
            self.assertEqual(captured["b"], {})
            self.assertEqual(set(captured["c"]), {"a"})
            self.assertEqual(result.graph_result.terminal_status, WorkGraphTerminalStatus.COMPLETED)

    async def test_missing_granted_unavailable_tool_rejects_before_executor_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            called = False

            async def executor(**kwargs: Any) -> dict[str, Any]:
                nonlocal called
                called = True
                return {}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                assignment_executor=executor,
                preflight_fn=_preflight(agentbay=False, image=False, video=False),
            )
            plan = _plan(
                [_assignment("a", "builder", required_capabilities=["implementation"], tool_ids=["execute_command"])],
                [WorkNode(id="a", assignment_id="a")],
            )
            result = await runtime.execute_plan(plan, "task", "request")
            self.assertFalse(called)
            self.assertEqual(result.graph_result.nodes["a"].status, WorkNodeStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["a"].attempts[0].code, "unavailable_tool_grant")

    async def test_public_report_and_events_redact_secrets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            events: list[tuple[str, dict[str, Any]]] = []

            async def executor(**kwargs: Any) -> dict[str, Any]:
                return {
                    "output_text": "see https://secret.example/video",
                    "provider_task_id": "task-123",
                    "session_id": "session-456",
                    "artifact_refs": ["artifact-a"],
                }

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                assignment_executor=executor,
                event_sink=lambda event_type, payload: events.append((event_type, dict(payload))),
                preflight_fn=_preflight(),
            )
            plan = _plan(
                [_assignment("a", "builder", required_capabilities=["implementation"], tool_ids=[])],
                [WorkNode(id="a", assignment_id="a")],
            )
            result = await runtime.execute_plan(plan, "task", "user request")
            public = result.node_outputs["a"]
            self.assertNotIn("provider_task_id", public)
            self.assertNotIn("session_id", public)
            self.assertIn("[redacted-url]", public["output_text"])
            serialized_events = str(events)
            self.assertNotIn("user request", serialized_events)
            self.assertIn("composition_assignment_materialized", serialized_events)


class AgnoAssignmentExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_validator_execution_uses_runtime_owned_cached_handle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            captured: dict[str, Any] = {"calls": 0}

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    del task_id, purpose
                    self._event_sink({"event_type": "agentbay_start_succeeded"})
                    return {"success": True, "data": {"handle": "runtime-owned"}}

                def run_code(self, handle: str, language: str, code: str, timeout_seconds: int) -> dict[str, Any]:
                    del language, code, timeout_seconds
                    captured["handle"] = handle
                    captured["calls"] += 1
                    self._event_sink({"event_type": "agentbay_run_code_succeeded"})
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    return [{"success": True}]

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    await self._tools["start_execution_environment"]("validate")
                    await self._tools["run_code"]("model-invented", "python", "assert True", 5)
                    captured["second_result"] = await self._tools["run_code"](
                        "model-invented", "python", "assert True", 5
                    )
                    return {"passed": True, "checks": ["real execution"], "failures": []}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            assignment = _assignment(
                "validator",
                "test_engineer",
                required_capabilities=["test_execution", "sandbox_execution"],
                tool_ids=["start_execution_environment", "run_code"],
            )
            from society.composition_runtime import _build_assignment_identity

            await runtime._assignment_executor(
                node=WorkNode(id="validator-node", assignment_id="validator"),
                assignment=assignment,
                materialized_agent=_build_assignment_identity(assignment),
                direct_dependency_outputs={},
                attempt=1,
                cancellation_event=asyncio.Event(),
            )

            self.assertEqual(captured["handle"], "runtime-owned")
            self.assertEqual(captured["calls"], 1)
            self.assertEqual(captured["second_result"]["error_code"], "sandbox_execution_limit_reached")

    def test_validator_prompt_requires_safe_real_execution_pattern(self) -> None:
        with TemporaryDirectory() as temp_dir:
            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
            )
            prompt = runtime._assignment_executor._build_prompt(
                assignment=_assignment(
                    "validator",
                    "test_engineer",
                    required_capabilities=["test_execution", "sandbox_execution", "artifact_inspection"],
                    tool_ids=["run_code", "inspect_artifact", "report_independent_validation"],
                ),
                tool_ids=["run_code", "inspect_artifact", "report_independent_validation"],
                skill_instructions=["verified skill"],
                direct_dependency_outputs={},
                user_request="validate",
            )

        self.assertIn("use pure Python built-ins", prompt)
        self.assertIn("Do not import modules", prompt)
        self.assertIn("Do not call pytest_target unless", prompt)

    def test_validator_compile_does_not_recover_failed_test_execution(self) -> None:
        assignment = _assignment(
            "validator",
            "test_engineer",
            required_capabilities=["test_execution", "sandbox_execution", "artifact_inspection"],
            tool_ids=["execute_command", "run_code"],
        )
        failed_then_compiled = [
            {"event_type": "agentbay_command_failed", "payload": {"command_id": "pytest_target"}},
            {"event_type": "agentbay_command_succeeded", "payload": {"command_id": "python_compile"}},
        ]

        self.assertIn(
            "sandbox_execution",
            AgnoAssignmentExecutor._failed_required_tool_outcomes(
                assignment,
                ["execute_command", "run_code"],
                failed_then_compiled,
            ),
        )
        self.assertIn(
            "sandbox_execution",
            AgnoAssignmentExecutor._missing_required_tool_evidence(
                assignment,
                ["execute_command", "run_code"],
                failed_then_compiled,
            ),
        )

        recovered = [
            *failed_then_compiled,
            {"event_type": "agentbay_run_code_succeeded", "payload": {}},
        ]
        self.assertNotIn(
            "sandbox_execution",
            AgnoAssignmentExecutor._failed_required_tool_outcomes(
                assignment,
                ["execute_command", "run_code"],
                recovered,
            ),
        )
        self.assertNotIn(
            "sandbox_execution",
            AgnoAssignmentExecutor._missing_required_tool_evidence(
                assignment,
                ["execute_command", "run_code"],
                recovered,
            ),
        )

    async def test_start_tool_wrapper_binds_internal_node_id_and_exposes_only_purpose(self) -> None:
        with TemporaryDirectory() as temp_dir:
            captured: dict[str, Any] = {}

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                async def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    captured["task_id"] = task_id
                    captured["purpose"] = purpose
                    self._event_sink({"event_type": "agentbay_start_succeeded", "request_id": "req-start"})
                    return {"success": True, "request_id": "req-start", "data": {"handle": "opaque"}}

                async def close_all(self) -> list[dict[str, Any]]:
                    captured["closed"] = True
                    return []

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = list(tools)

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    start_tool = self._tools[0]
                    signature = inspect.signature(start_tool)
                    assert list(signature.parameters) == ["purpose"]
                    result = await start_tool("  compile\nproject " + ("x" * 240))
                    assert result["success"] is True
                    return {"artifact_refs": []}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            assignment = _assignment(
                "builder",
                "builder",
                required_capabilities=["implementation"],
                tool_ids=["start_execution_environment", "close_execution_environment"],
            )
            from society.composition_runtime import _build_assignment_identity
            materialized = _build_assignment_identity(assignment)
            await runtime._assignment_executor(
                node=WorkNode(id="builder-node", assignment_id="builder"),
                assignment=assignment,
                materialized_agent=materialized,
                direct_dependency_outputs={},
                attempt=1,
                cancellation_event=asyncio.Event(),
            )
            self.assertEqual(captured["task_id"], "builder-node")
            self.assertEqual(len(captured["purpose"]), 200)
            self.assertTrue(captured["purpose"].startswith("compile project "))
            self.assertNotIn("\n", captured["purpose"])
            self.assertTrue(captured["closed"])

    async def test_filtered_tool_exposure_passes_only_granted_methods(self) -> None:
        with TemporaryDirectory() as temp_dir:
            seen_tools: list[str] = []

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._closed = False

                def generate_images(self) -> None:
                    return None

                def inspect_image(self) -> None:
                    return None

                def publish_image(self) -> None:
                    return None

                def extra_method(self) -> None:
                    return None

            class FakeAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    return {"prompt_seen": prompt}

            def build_agent(identity: Any, settings: Settings, **kwargs: Any) -> FakeAgent:
                del identity, settings
                seen_tools.extend(sorted(getattr(tool, "__name__", type(tool).__name__) for tool in kwargs["tools"]))
                return FakeAgent()

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=False, image=True, video=False),
                build_agent_factory=build_agent,
                image_toolkit_factory=FakeToolkit,
            )
            executor = runtime._assignment_executor
            assignment = _assignment(
                "img",
                "image_creator",
                required_capabilities=["image_generation"],
                tool_ids=["generate_images", "inspect_image"],
            )
            materialized = runtime._build_assignment_identity(assignment) if hasattr(runtime, "_build_assignment_identity") else None
            if materialized is None:
                from society.composition_runtime import _build_assignment_identity
                materialized = _build_assignment_identity(assignment)
            result = await executor(
                node=WorkNode(id="img", assignment_id="img"),
                assignment=assignment,
                materialized_agent=materialized,
                direct_dependency_outputs={},
                attempt=1,
                cancellation_event=asyncio.Event(),
            )
            self.assertEqual(set(seen_tools), {"generate_images", "inspect_image"})
            self.assertEqual(result["prompt_seen"].startswith("Complete the assignment"), True)

    async def test_local_artifact_tools_are_only_exposed_when_granted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            seen_tools: list[str] = []
            artifact = Path(temp_dir) / "agentbay" / "exported.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("hello", encoding="utf-8")

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    inspected = self._tools["inspect_artifact"](str(artifact))
                    assert inspected["success"] is True
                    report = self._tools["report_independent_validation"](
                        True,
                        ["artifact readable"],
                        [],
                        [inspected["artifact_ref"]],
                        "passed",
                    )
                    assert report["passed"] is True
                    return {
                        "artifact_refs": [str(artifact)],
                        "passed": True,
                        "checks": ["artifact readable"],
                        "failures": [],
                        "inspected_artifact_refs": [inspected["artifact_ref"]],
                    }

            def build_agent(identity: Any, settings: Settings, **kwargs: Any) -> FakeAgent:
                del identity, settings
                seen_tools.extend(sorted(getattr(tool, "__name__", type(tool).__name__) for tool in kwargs["tools"]))
                return FakeAgent(kwargs["tools"])

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=False, image=False, video=False),
                build_agent_factory=build_agent,
            )
            assignment = _assignment(
                "validator",
                "test_engineer",
                required_capabilities=["test_execution"],
                tool_ids=["inspect_artifact", "report_independent_validation"],
            )
            from society.composition_runtime import _build_assignment_identity
            materialized = _build_assignment_identity(assignment)
            await runtime._assignment_executor(
                node=WorkNode(id="validator", assignment_id="validator"),
                assignment=assignment,
                materialized_agent=materialized,
                direct_dependency_outputs={},
                attempt=1,
                cancellation_event=asyncio.Event(),
            )
            self.assertEqual(set(seen_tools), {"inspect_artifact", "report_independent_validation"})

    async def test_negative_independent_validation_fails_the_assignment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "agentbay" / "exported.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("bad", encoding="utf-8")

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    inspected = self._tools["inspect_artifact"](str(artifact))
                    self._tools["report_independent_validation"](
                        False,
                        ["content checked"],
                        ["content is invalid"],
                        [inspected["artifact_ref"]],
                        "failed",
                    )
                    return {"passed": False, "failures": ["content is invalid"]}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=False, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
            )
            assignment = _assignment(
                "validator",
                "test_engineer",
                required_capabilities=["test_execution"],
                tool_ids=["inspect_artifact", "report_independent_validation"],
            )
            from society.composition_runtime import _build_assignment_identity

            with self.assertRaises(NodeExecutionError) as exc_info:
                await runtime._assignment_executor(
                    node=WorkNode(id="validator", assignment_id="validator"),
                    assignment=assignment,
                    materialized_agent=_build_assignment_identity(assignment),
                    direct_dependency_outputs={},
                    attempt=1,
                    cancellation_event=asyncio.Event(),
                )

            self.assertEqual(exc_info.exception.code, "independent_validation_failed")

    async def test_prompt_requests_strict_json_with_artifact_refs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            prompt_seen: list[str] = []
            artifact = Path(temp_dir) / "agentbay" / "exported.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("hello", encoding="utf-8")

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    prompt_seen.append(prompt)
                    inspected = self._tools["inspect_artifact"](str(artifact))
                    report = self._tools["report_independent_validation"](
                        True,
                        ["checked"],
                        [],
                        [inspected["artifact_ref"]],
                        "ok",
                    )
                    return {
                        "artifact_refs": [],
                        "passed": report["passed"],
                        "checks": report["checks"],
                        "failures": report["failures"],
                        "inspected_artifact_refs": report["inspected_artifact_refs"],
                    }

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=False, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
            )
            assignment = _assignment(
                "validator",
                "test_engineer",
                required_capabilities=["test_execution"],
                tool_ids=["inspect_artifact", "report_independent_validation"],
            )
            from society.composition_runtime import _build_assignment_identity
            materialized = _build_assignment_identity(assignment)
            await runtime._assignment_executor(
                node=WorkNode(id="validator", assignment_id="validator"),
                assignment=assignment,
                materialized_agent=materialized,
                direct_dependency_outputs={"build": {"artifact_refs": ["agentbay/calc.py"]}},
                attempt=1,
                cancellation_event=asyncio.Event(),
            )
            self.assertIn("Return exactly one JSON object", prompt_seen[0])
            self.assertIn("Always include an `artifact_refs` array", prompt_seen[0])
            self.assertIn("validator assignments", prompt_seen[0])

    async def test_narrated_success_without_required_tool_calls_fails_node(self) -> None:
        with TemporaryDirectory() as temp_dir:
            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    pass

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    del task_id, purpose
                    return {"success": True}

                def execute_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"success": True}

                def export_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"success": True}

                def close_execution_environment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    return [{"success": True, "data": {"closed": True}}]

            class FakeAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    return {"artifact_refs": ["agentbay/exported_calc.py"], "summary": "completed"}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(),
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
                    )
                ],
                [WorkNode(id="builder-node", assignment_id="builder")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(result.graph_result.terminal_status, WorkGraphTerminalStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["builder-node"].status, WorkNodeStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["builder-node"].attempts[0].code, "mandatory_tool_evidence_missing")

    async def test_repeated_bound_start_reuses_one_create_and_returns_same_handle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            start_calls = 0
            direct_close_calls = 0
            close_all_calls = 0

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    nonlocal start_calls
                    del task_id, purpose
                    start_calls += 1
                    self._event_sink({"event_type": "agentbay_start_succeeded", "request_id": "req-start"})
                    return {"success": True, "data": {"handle": "opaque-handle"}}

                def execute_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_command_succeeded", "request_id": "req-cmd"})
                    return {"success": True}

                def export_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink(
                        {
                            "event_type": "agentbay_artifact_exported",
                            "artifact_ref": {"path": "agentbay/exported_calc.py", "sha256": "abc", "size_bytes": 12},
                        }
                    )
                    return {"success": True}

                def close_execution_environment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    nonlocal direct_close_calls
                    direct_close_calls += 1
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    nonlocal close_all_calls
                    close_all_calls += 1
                    return [{"success": True, "data": {"closed": True}}]

            class FakeAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    first = await self._tools["start_execution_environment"]("first")
                    second = await self._tools["start_execution_environment"]("second")
                    assert first["data"]["handle"] == second["data"]["handle"]
                    assert second["data"]["idempotent"] is True
                    deferred = await self._tools["close_execution_environment"](second["data"]["handle"])
                    assert deferred["success"] is True
                    assert deferred["data"]["closed"] is False
                    assert deferred["data"]["deferred"] is True
                    self._tools["execute_command"](second["data"]["handle"], "pytest_target", {"target": "ok.py"}, 5)
                    self._tools["export_artifact"](second["data"]["handle"], "/workspace/calc.py", "code")
                    return {"artifact_refs": ["agentbay/exported_calc.py"], "summary": "completed"}

                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
                    )
                ],
                [WorkNode(id="builder-node", assignment_id="builder")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(start_calls, 1)
            self.assertEqual(direct_close_calls, 0)
            self.assertEqual(close_all_calls, 1)
            self.assertEqual(result.graph_result.nodes["builder-node"].status, WorkNodeStatus.COMPLETED)
            self.assertEqual(result.node_artifact_refs["builder-node"], ["agentbay/exported_calc.py"])

    async def test_cached_failed_start_never_recreates_environment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            start_calls = 0

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    nonlocal start_calls
                    del task_id, purpose
                    start_calls += 1
                    self._event_sink({"event_type": "agentbay_start_failed", "error": "provider create failed"})
                    return {"success": False, "error_code": "agentbay_create_failed", "error_message": "provider create failed"}

                def close_all(self) -> list[dict[str, Any]]:
                    return []

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    first = await self._tools["start_execution_environment"]("first")
                    second = await self._tools["start_execution_environment"]("second")
                    assert first["success"] is False
                    assert second["success"] is False
                    return {"summary": "failed start cached"}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
                    )
                ],
                [WorkNode(id="builder-node", assignment_id="builder")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(start_calls, 1)
            self.assertEqual(result.graph_result.nodes["builder-node"].status, WorkNodeStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["builder-node"].attempts[0].code, "mandatory_tool_reported_failure")

    async def test_sandbox_failure_then_success_recovers_mandatory_capability(self) -> None:
        with TemporaryDirectory() as temp_dir:
            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    del task_id, purpose
                    self._event_sink({"event_type": "agentbay_start_succeeded", "request_id": "req-start"})
                    return {"success": True, "data": {"handle": "opaque"}}

                def execute_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_run_code_failed", "error": "restricted import"})
                    return {"success": False, "error_message": "restricted import"}

                def run_code(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_run_code_succeeded", "request_id": "req-run"})
                    return {"success": True}

                def export_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_artifact_exported", "artifact_ref": {"path": "agentbay/exported_calc.py"}})
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    return [{"success": True, "data": {"closed": True}}]

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    started = await self._tools["start_execution_environment"]("build")
                    handle = started["data"]["handle"]
                    self._tools["execute_command"](handle, "pytest_target", {"target": "bad.py"}, 5)
                    self._tools["run_code"](handle, "python", "print(2+2)", 5)
                    self._tools["export_artifact"](handle, "/workspace/calc.py", "code")
                    return {"summary": "recovered", "artifact_refs": []}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "run_code", "export_artifact", "close_execution_environment"],
                    )
                ],
                [WorkNode(id="builder-node", assignment_id="builder")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(result.graph_result.nodes["builder-node"].status, WorkNodeStatus.COMPLETED)
            self.assertIn("agentbay/exported_calc.py", result.node_artifact_refs["builder-node"])

    async def test_sandbox_success_then_later_failure_is_still_fatal(self) -> None:
        with TemporaryDirectory() as temp_dir:
            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    del task_id, purpose
                    self._event_sink({"event_type": "agentbay_start_succeeded"})
                    return {"success": True, "data": {"handle": "opaque"}}

                def execute_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_command_succeeded", "request_id": "req-cmd"})
                    return {"success": True}

                def run_code(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_run_code_failed", "error": "later failure"})
                    return {"success": False, "error_message": "later failure"}

                def export_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_artifact_exported", "artifact_ref": {"path": "agentbay/exported_calc.py"}})
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    return [{"success": True, "data": {"closed": True}}]

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    handle = (await self._tools["start_execution_environment"]("build"))["data"]["handle"]
                    self._tools["execute_command"](handle, "pytest_target", {"target": "ok.py"}, 5)
                    self._tools["run_code"](handle, "python", "import os", 5)
                    self._tools["export_artifact"](handle, "/workspace/calc.py", "code")
                    return {"summary": "later failure", "artifact_refs": []}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "run_code", "export_artifact", "close_execution_environment"],
                    )
                ],
                [WorkNode(id="builder-node", assignment_id="builder")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(result.graph_result.nodes["builder-node"].status, WorkNodeStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["builder-node"].attempts[0].code, "mandatory_tool_reported_failure")

    async def test_event_backed_artifact_refs_are_merged_when_model_omits_them(self) -> None:
        with TemporaryDirectory() as temp_dir:
            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    del task_id, purpose
                    self._event_sink({"event_type": "agentbay_start_succeeded"})
                    return {"success": True, "data": {"handle": "opaque"}}

                def execute_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_command_succeeded"})
                    return {"success": True}

                def export_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink(
                        {
                            "event_type": "agentbay_artifact_exported",
                            "artifact_ref": {"path": "agentbay/exported_calc.py", "id": "artifact-123"},
                        }
                    )
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    return [{"success": True, "data": {"closed": True}}]

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    handle = (await self._tools["start_execution_environment"]("build"))["data"]["handle"]
                    self._tools["execute_command"](handle, "pytest_target", {"target": "ok.py"}, 5)
                    self._tools["export_artifact"](handle, "/workspace/calc.py", "code")
                    return {"summary": "artifact omitted"}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
                    )
                ],
                [WorkNode(id="builder-node", assignment_id="builder")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(result.node_outputs["builder-node"]["artifact_refs"], ["agentbay/exported_calc.py"])
            self.assertEqual(result.node_artifact_refs["builder-node"], ["agentbay/exported_calc.py"])

    async def test_event_backed_negative_validation_fails_when_model_omits_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "calc.py"
            artifact.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

            class FakeAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    inspected = self._tools["inspect_artifact"](str(artifact))
                    self._tools["report_independent_validation"](
                        False,
                        ["add(2, 3) returned 5"],
                        ["edge case not covered"],
                        [inspected["artifact_ref"]],
                        "validation failed",
                    )
                    return {"summary": "validator omitted structured fields"}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=False, image=False, video=False),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(kwargs["tools"]),
            )
            plan = _plan(
                [
                    _assignment(
                        "validator",
                        "test_engineer",
                        required_capabilities=["artifact_inspection"],
                        tool_ids=["inspect_artifact", "report_independent_validation"],
                    )
                ],
                [WorkNode(id="validator-node", assignment_id="validator")],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            node_result = result.graph_result.nodes["validator-node"]
            self.assertEqual(node_result.status, WorkNodeStatus.FAILED)
            self.assertEqual(node_result.attempts[0].code, "independent_validation_failed")

    async def test_failed_builder_blocks_dependent_test_engineer_before_second_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            starts: list[str] = []

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    self._role_key = kwargs["role_key"]
                    self._event_sink = kwargs["event_sink"]

                def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
                    starts.append(self._role_key)
                    self._event_sink({"event_type": "agentbay_start_succeeded", "request_id": f"req-{self._role_key}"})
                    return {"success": True, "data": {"handle": "opaque"}}

                def execute_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    self._event_sink({"event_type": "agentbay_command_succeeded", "request_id": f"cmd-{self._role_key}"})
                    return {"success": True}

                def export_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"success": True}

                def inspect_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"success": True}

                def report_independent_validation(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"passed": True}

                def close_execution_environment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                    return {"success": True}

                def close_all(self) -> list[dict[str, Any]]:
                    return [{"success": True, "data": {"closed": True}}]

            class BuilderAgent:
                def __init__(self, tools: list[Any]) -> None:
                    self._tools = {getattr(tool, "__name__", type(tool).__name__): tool for tool in tools}

                async def arun(self, prompt: str) -> dict[str, Any]:
                    del prompt
                    await self._tools["start_execution_environment"]("build artifact")
                    self._tools["execute_command"]("opaque", "pytest_target", {"target": "ok.py"}, 5)
                    return {"artifact_refs": ["agentbay/exported_calc.py"], "summary": "done without export evidence"}

            class TestAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    raise AssertionError("Dependent validator should not run when builder fails")

            def build_agent(identity: Any, settings: Settings, **kwargs: Any) -> Any:
                del settings
                return BuilderAgent(kwargs["tools"]) if identity.role == "builder" else TestAgent()

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(agentbay=True, image=False, video=False),
                build_agent_factory=build_agent,
                agentbay_toolkit_factory=FakeToolkit,
            )
            plan = _plan(
                [
                    _assignment(
                        "builder",
                        "builder",
                        required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                        tool_ids=["start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
                    ),
                    _assignment(
                        "validator",
                        "test_engineer",
                        required_capabilities=["test_execution", "sandbox_execution", "artifact_inspection"],
                        tool_ids=["start_execution_environment", "execute_command", "inspect_artifact", "report_independent_validation", "close_execution_environment"],
                    ),
                ],
                [
                    WorkNode(id="builder-node", assignment_id="builder"),
                    WorkNode(id="test-node", assignment_id="validator", depends_on=["builder-node"]),
                ],
            )

            result = await runtime.execute_plan(plan, "task", "request")

            self.assertEqual(result.graph_result.nodes["builder-node"].status, WorkNodeStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["test-node"].status, WorkNodeStatus.BLOCKED)
            self.assertEqual(starts, ["builder"])

    async def test_agentbay_close_all_runs_on_success_failure_and_cancellation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            close_counts: list[int] = []

            class FakeToolkit:
                def __init__(self, **kwargs: Any) -> None:
                    pass

                def start_execution_environment(self) -> None:
                    return None

                def execute_command(self) -> None:
                    return None

                def export_artifact(self) -> None:
                    return None

                def close_execution_environment(self) -> None:
                    return None

                def close_all(self) -> list[dict[str, Any]]:
                    close_counts.append(1)
                    return [{"success": True}]

            class SuccessAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    return {"ok": True}

            class FailingAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    raise RuntimeError("boom")

            class CancelingAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    raise asyncio.CancelledError()

            def build_agent_factory_factory(agent_class: type[Any]):
                def factory(identity: Any, settings: Settings, **kwargs: Any) -> Any:
                    del identity, settings, kwargs
                    return agent_class()
                return factory

            assignment = _assignment(
                "builder",
                "builder",
                required_capabilities=["implementation"],
                tool_ids=["start_execution_environment", "execute_command", "export_artifact", "close_execution_environment"],
            )
            from society.composition_runtime import _build_assignment_identity
            materialized = _build_assignment_identity(assignment)

            for agent_class in (SuccessAgent, FailingAgent, CancelingAgent):
                runtime = CompositionRuntime(
                    _settings(),
                    temp_dir,
                    preflight_fn=_preflight(agentbay=True, image=False, video=False),
                    build_agent_factory=build_agent_factory_factory(agent_class),
                    agentbay_toolkit_factory=FakeToolkit,
                )
                executor = runtime._assignment_executor
                try:
                    await executor(
                        node=WorkNode(id="builder", assignment_id="builder"),
                        assignment=assignment,
                        materialized_agent=materialized,
                        direct_dependency_outputs={},
                        attempt=1,
                        cancellation_event=asyncio.Event(),
                    )
                except (RuntimeError, asyncio.CancelledError):
                    pass
            self.assertEqual(len(close_counts), 3)

    async def test_media_store_is_shared_between_image_and_video_factories(self) -> None:
        with TemporaryDirectory() as temp_dir:
            stores: list[Any] = []

            class FakeImageToolkit:
                def __init__(self, *, artifact_store: Any, **kwargs: Any) -> None:
                    stores.append(artifact_store)

                def generate_images(self) -> None:
                    return None

            class FakeVideoToolkit:
                def __init__(self, *, artifact_store: Any, **kwargs: Any) -> None:
                    stores.append(artifact_store)

                def submit_text_to_video(self) -> None:
                    return None

            class FakeAgent:
                async def arun(self, prompt: str) -> dict[str, Any]:
                    return {}

            runtime = CompositionRuntime(
                _settings(),
                temp_dir,
                preflight_fn=_preflight(),
                build_agent_factory=lambda identity, settings, **kwargs: FakeAgent(),
                image_toolkit_factory=FakeImageToolkit,
                video_toolkit_factory=FakeVideoToolkit,
            )
            plan = _plan(
                [
                    _assignment("image", "image_creator", required_capabilities=["image_generation"], tool_ids=["generate_images"]),
                    _assignment("video", "video_producer", required_capabilities=["video_generation"], tool_ids=["submit_text_to_video"]),
                ],
                [
                    WorkNode(id="image", assignment_id="image"),
                    WorkNode(id="video", assignment_id="video"),
                ],
            )
            await runtime.execute_plan(plan, "task", "request")
            self.assertEqual(len(stores), 2)
            self.assertIs(stores[0], stores[1])

    async def test_default_executor_refuses_missing_llm_instead_of_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(LLM_PROVIDER="qwen", QWEN_API_KEY="")
            runtime = CompositionRuntime(settings, temp_dir, preflight_fn=_preflight())
            plan = _plan(
                [_assignment("a", "builder", required_capabilities=["implementation"], tool_ids=[])],
                [WorkNode(id="a", assignment_id="a")],
            )
            result = await runtime.execute_plan(plan, "task", "request")
            self.assertEqual(result.graph_result.nodes["a"].status, WorkNodeStatus.FAILED)
            self.assertEqual(result.graph_result.nodes["a"].attempts[0].code, "llm_unavailable")


if __name__ == "__main__":
    unittest.main()
