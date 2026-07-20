"""Provider-backed outcome_v4 adapter tests with mocked Qwen and AgentBay seams."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agno.tools.function import Function
from config import Settings
from benchmarks.outcome_v4.adapters import (
    ProviderAdapterRefusal,
    ProviderBackedSingleAgentAdapter,
    ProviderBackedSocietyAdapter,
    _debit_usage,
    _record_staging_activity,
    _single_agent_prompt,
    _single_agent_tools,
    _society_user_request,
    _usage_charge_delta,
    _assert_completed_society_graph,
    _resolve_provider_specialist_selection,
    _write_society_execution_diagnostic,
    _write_society_interruption_diagnostic,
)
from benchmarks.outcome_v4.budget import AtomicBudgetLedger
from benchmarks.outcome_v4.evaluators import evaluate_attempt
from benchmarks.outcome_v4.fixtures import FIXTURES_ROOT, load_public_scenario
from benchmarks.outcome_v4.models import BudgetCaps, RetryPolicy
from benchmarks.outcome_v4.runner import build_prompt, run_attempt
from society.composition_runtime import MaterializedAgentSummary, RuntimeAvailability
from society.capability_registry import list_fixed_specialist_templates
from society.schemas.team_composition import TeamAssignment, TeamCompositionPlan, ToolGrant, WorkNode
from society.specialist_selection import (
    FixedSpecialistCoordinator,
    FixedSpecialistProviderError,
    SelectSpecialistsCall,
    SpecialistAssignmentSelection,
)
from society.team_composer import CompositionContext
from society.work_graph import WorkGraphExecutionResult, WorkGraphTerminalStatus, WorkNodeAttemptRecord, WorkNodeRuntimeRecord, WorkNodeStatus


def _settings_factory(_: RetryPolicy) -> Settings:
    return Settings(
        LLM_PROVIDER="qwen",
        QWEN_API_KEY="sk-test",
        QWEN_MODEL="qwen3.7-plus",
        AGENTBAY_API_KEY="agentbay",
        AGENTBAY_ENDPOINT="wuyingai.ap-southeast-1.aliyuncs.com",
        AGENTBAY_REGION_ID="ap-southeast-1",
        AGENTBAY_IMAGE_ID="code_latest",
        DASHSCOPE_API_KEY="dashscope",
        MODEL_STUDIO_WORKSPACE_ID="workspace",
    )


def _ready_preflight(_settings: Settings) -> dict[str, object]:
    return {
        "provider": "qwen",
        "model": "qwen3.7-plus",
        "typed_blockers": [],
        "provider_services": {
            "agentbay": {"ready": True},
            "browser": {"ready": True},
            "image": {"ready": True},
            "video": {"ready": False},
        },
    }


def _incident_exports(export_root: Path) -> tuple[list[dict[str, str]], list[str]]:
    required_paths = [
        "repo/services/api/auth.py",
        "repo/services/worker/idempotency.py",
        "repo/services/config/database.py",
        "repo/requirements.txt",
        "reports/test_report.json",
        "reports/security_report.json",
        "reports/evidence_report.json",
        "incident/rollback_plan.json",
    ]
    contents = {
        "repo/services/api/auth.py": "def allow_support_scope(scope: str, token_enabled: bool) -> bool:\n    return token_enabled and scope in {\"admin\", \"support\"}\n",
        "repo/services/worker/idempotency.py": "processed_ids: set[str] = set()\n\ndef process_job(job_id: str) -> str:\n    if job_id in processed_ids:\n        return \"duplicate_ignored\"\n    processed_ids.add(job_id)\n    return \"processed\"\n",
        "repo/services/config/database.py": "import os\n\ndef database_url() -> str:\n    return os.getenv(\"DB_URL\", \"postgresql://localhost/devdb\")\n",
        "repo/requirements.txt": "pyyaml==6.0.2\n",
        "reports/test_report.json": json.dumps({"passed": True, "tests_run": 6, "truthful_failures_retained": True}, indent=2, sort_keys=True),
        "reports/security_report.json": json.dumps({"passed": True, "reachable_vulnerability_closed": True, "truthful_failures_retained": True}, indent=2, sort_keys=True),
        "reports/evidence_report.json": json.dumps(
            {
                "evidence_ids": ["AUTH-LOG-401", "DB-CONFIG-URL", "JOB-TRACE-008"],
                "changed_files": ["auth.py", "idempotency.py", "database.py", "requirements.txt"],
                "command_evidence": ["pytest -q", "pip-audit --strict"],
            },
            indent=2,
            sort_keys=True,
        ),
        "incident/rollback_plan.json": json.dumps({"max_rollback_minutes": 15, "validated": True}, indent=2, sort_keys=True),
    }
    exports: list[dict[str, str]] = []
    artifact_refs: list[str] = []
    for relative_path in required_paths:
        target = export_root / Path(relative_path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents[relative_path], encoding="utf-8")
        exports.append({"workspace_relative_path": relative_path, "artifact_ref": str(target)})
        artifact_refs.append(str(target))
    return exports, artifact_refs


class _FakeMetrics:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return dict(self._payload)


class _FakeResponse:
    def __init__(self, content: str, metrics: dict[str, object]) -> None:
        self.content = content
        self.metrics = _FakeMetrics(metrics)
        self.model = "qwen3.7-plus"
        self.model_provider = "qwen"


class _SleepingAgent:
    async def arun(self, prompt: str) -> object:
        del prompt
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _ToolkitRecorder:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.close_all_calls = 0
        self.start_calls = 0
        self.workspace_source_dir = kwargs.get("workspace_source_dir")

    def close_all(self) -> list[dict[str, object]]:
        self.close_all_calls += 1
        return [{"success": True, "request_id": "req-close", "data": {"closed": True}}]

    def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, object]:
        del task_id, purpose
        self.start_calls += 1
        return {"success": True, "request_id": "req-start", "data": {"handle": "handle-1"}}


class _FailingCloseToolkitRecorder(_ToolkitRecorder):
    def close_all(self) -> list[dict[str, object]]:
        self.close_all_calls += 1
        raise RuntimeError("cleanup boom")


class _SchemaToolkitRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, object]:
        self.calls.append(("start_execution_environment", {"task_id": task_id, "purpose": purpose}))
        return {"success": True, "request_id": "req-start", "data": {"handle": "handle-1"}}

    def execute_command(self, handle: str, command_id: str, arguments: dict[str, object] | None, timeout_seconds: int) -> dict[str, object]:
        self.calls.append(
            (
                "execute_command",
                {
                    "handle": handle,
                    "command_id": command_id,
                    "arguments": arguments,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        return {"success": True}

    def run_code(self, handle: str, language: str, code: str, timeout_seconds: int) -> dict[str, object]:
        self.calls.append(
            (
                "run_code",
                {
                    "handle": handle,
                    "language": language,
                    "code": code,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        return {"success": True}

    def read_text_file(self, handle: str, path: str, offset: int = 0, length: int = 65536) -> dict[str, object]:
        self.calls.append(
            (
                "read_text_file",
                {"handle": handle, "path": path, "offset": offset, "length": length},
            )
        )
        return {"success": True}

    def write_text_file(self, handle: str, path: str, content: str, mode: str = "overwrite") -> dict[str, object]:
        self.calls.append(
            (
                "write_text_file",
                {"handle": handle, "path": path, "content": content, "mode": mode},
            )
        )
        return {"success": True}

    def list_files(self, handle: str, path: str) -> dict[str, object]:
        self.calls.append(("list_files", {"handle": handle, "path": path}))
        return {"success": True}

    def export_artifact(self, handle: str, path: str, artifact_kind: str) -> dict[str, object]:
        self.calls.append(
            (
                "export_artifact",
                {"handle": handle, "path": path, "artifact_kind": artifact_kind},
            )
        )
        return {"success": True}

    def browser_render(self, handle: str, entry_html_path: str = "/workspace/app/dist/index.html") -> dict[str, object]:
        self.calls.append(
            (
                "browser_render",
                {"handle": handle, "entry_html_path": entry_html_path},
            )
        )
        return {"success": True}

    def close_execution_environment(self, handle: str) -> dict[str, object]:
        self.calls.append(("close_execution_environment", {"handle": handle}))
        return {"success": True}

    def close_all(self) -> list[dict[str, object]]:
        return []


class OutcomeV4ProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_society_interruption_diagnostic_records_cleanup_without_private_ids(self) -> None:
        plan = TeamCompositionPlan(
            task_summary="repair",
            assignments=[],
            work_graph=[],
            selection_rationale="bounded test",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "attempt" / "workspace"
            workspace.mkdir(parents=True)
            path = _write_society_interruption_diagnostic(
                workspace,
                plan=plan,
                runtime_events=[{
                    "event_type": "composition_assignment_cleanup_completed",
                    "session_id": "must-not-persist",
                }],
                error=TimeoutError(),
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["cleanup_observed"])
            self.assertEqual(payload["runtime_event_counts"], {"composition_assignment_cleanup_completed": 1})
            self.assertNotIn("must-not-persist", path.read_text(encoding="utf-8"))

    async def test_malformed_leader_selection_receives_one_bounded_correction(self) -> None:
        calls: list[dict[str, object]] = []

        class FlakyProvider:
            async def propose(self, **kwargs: object) -> SelectSpecialistsCall:
                calls.append(dict(kwargs))
                if len(calls) == 1:
                    raise FixedSpecialistProviderError("unknown_dependency:validate:[']build']")
                return SelectSpecialistsCall(
                    assignments=[
                        SpecialistAssignmentSelection(
                            assignment_id="build",
                            template_id="builder",
                            objective="Build and export the repair.",
                            depends_on=[],
                            owned_artifacts=["src/calc.py"],
                            acceptance_requirements=["unit_tests"],
                        ),
                        SpecialistAssignmentSelection(
                            assignment_id="validate",
                            template_id="test_engineer",
                            objective="Independently validate the repair.",
                            depends_on=["build"],
                            owned_artifacts=[],
                            acceptance_requirements=["unit_tests"],
                        ),
                    ],
                    selection_rationale="A separate validator checks the builder output.",
                )

        all_tools = sorted({
            tool_id
            for template in list_fixed_specialist_templates().values()
            for tool_id in template.tool_ids
        })
        coordinator = FixedSpecialistCoordinator(all_tools)
        context = SimpleNamespace(
            task_summary="Repair a repository",
            user_request="Repair and validate it",
            acceptance_requirements=["unit_tests"],
            required_capabilities=["implementation", "test_execution"],
        )

        resolved = await _resolve_provider_specialist_selection(
            provider=FlakyProvider(),
            coordinator=coordinator,
            context=context,
            catalog=coordinator.list_specialists(),
            required_artifacts=["src/calc.py"],
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["attempt_number"], 2)
        self.assertEqual(calls[1]["prior_blockers"][0]["code"], "fixed_specialist_provider_malformed")
        self.assertEqual([item.assignment_id for item in resolved.assignments], ["build", "validate"])

    def test_failed_society_graph_persists_actionable_secret_safe_diagnostic(self) -> None:
        plan = TeamCompositionPlan(
            task_summary="repair",
            assignments=[],
            work_graph=[],
            selection_rationale="bounded test",
        )
        graph_result = WorkGraphExecutionResult(
            terminal_status=WorkGraphTerminalStatus.FAILED,
            started_at=0.0,
            finished_at=1.0,
            duration_seconds=1.0,
            nodes={
                "builder-node": WorkNodeRuntimeRecord(
                    node_id="builder-node",
                    assignment_id="builder",
                    status=WorkNodeStatus.FAILED,
                    attempts=[
                        WorkNodeAttemptRecord(
                            attempt=1,
                            status=WorkNodeStatus.FAILED,
                            started_at=0.0,
                            finished_at=1.0,
                            duration_seconds=1.0,
                            category="validation",
                            code="required_artifacts_missing",
                            message="missing reports/test_report.json",
                        )
                    ],
                )
            },
        )
        execution = SimpleNamespace(
            graph_result=graph_result,
            materialized_agents=[],
            node_outputs={},
            availability_blockers=[],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "attempt" / "workspace"
            workspace.mkdir(parents=True)
            diagnostic = _write_society_execution_diagnostic(
                workspace,
                plan=plan,
                execution=execution,
                runtime_events=[
                    {"event_type": "agentbay_start_succeeded", "session_id": "must-not-persist"},
                    {"event_type": "agentbay_start_succeeded", "request_id": "also-private"},
                ],
            )

            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            self.assertEqual(payload["runtime_event_counts"], {"agentbay_start_succeeded": 2})
            self.assertNotIn("must-not-persist", diagnostic.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(RuntimeError, "required_artifacts_missing.*society_execution_diagnostic.json"):
                _assert_completed_society_graph(graph_result, diagnostic)

    async def test_provider_usage_charge_delta_normalizes_cumulative_metrics(self) -> None:
        first = {
            "actor_id": "agent-1",
            "call_kind": "single_agent",
            "input_tokens": 120,
            "output_tokens": 60,
        }
        second = {
            "actor_id": "agent-1",
            "call_kind": "single_agent",
            "input_tokens": 180,
            "output_tokens": 90,
        }
        self.assertEqual(
            _usage_charge_delta(first, []),
            {"input_tokens": 120, "output_tokens": 60, "total_tokens": 180, "normalization": "direct"},
        )
        self.assertEqual(
            _usage_charge_delta(second, [first]),
            {"input_tokens": 60, "output_tokens": 30, "total_tokens": 90, "normalization": "cumulative_delta"},
        )

    async def test_provider_usage_debits_once_when_metrics_are_cumulative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            usage_history: list[dict[str, object]] = []
            first: dict[str, object] = {
                "actor_id": "agent-1",
                "call_kind": "single_agent",
                "input_tokens": 120,
                "output_tokens": 60,
            }
            await _debit_usage(ledger, first, usage_history)  # type: ignore[arg-type]
            usage_history.append(first)
            second: dict[str, object] = {
                "actor_id": "agent-1",
                "call_kind": "single_agent",
                "input_tokens": 180,
                "output_tokens": 90,
            }
            await _debit_usage(ledger, second, usage_history)  # type: ignore[arg-type]
            usage_history.append(second)

            snapshot = ledger.snapshot()
            self.assertEqual(snapshot.qwen_input_tokens, 180)
            self.assertEqual(snapshot.qwen_output_tokens, 90)
            self.assertEqual(snapshot.qwen_total_tokens, 270)
            self.assertEqual(first["charged_input_tokens"], 120)
            self.assertEqual(second["charged_input_tokens"], 60)
            self.assertEqual(second["charged_output_tokens"], 30)
            self.assertEqual(second["charge_normalization"], "cumulative_delta")

    async def test_single_agent_provider_wires_full_union_exports_workspace_and_cleans_up(self) -> None:
        scenario = load_public_scenario("incident_repair")
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            shutil.copytree(FIXTURES_ROOT / "incident_repair" / "dev", workspace)
            export_root = workspace / "_provider_artifacts" / "single_agent"
            workspace_exports, artifact_refs = _incident_exports(export_root)
            tool_names: list[str] = []
            toolkit_instances: list[_ToolkitRecorder] = []

            def toolkit_factory(**kwargs: object) -> _ToolkitRecorder:
                toolkit = _ToolkitRecorder(**kwargs)
                toolkit_instances.append(toolkit)
                return toolkit

            def build_agent_factory(identity: object, settings: object, **kwargs: object) -> object:
                nonlocal tool_names
                tool_names = [getattr(tool, "__name__", type(tool).__name__) for tool in kwargs["tools"]]
                tools_by_name = {getattr(tool, "__name__", type(tool).__name__): tool for tool in kwargs["tools"]}

                class _Agent:
                    async def arun(self, prompt: str) -> _FakeResponse:
                        self.last_prompt = prompt
                        started = await tools_by_name["start_execution_environment"](purpose="repair fixture")
                        assert started["success"] is True
                        return _FakeResponse(
                            json.dumps(
                                {
                                    "summary": "incident repaired",
                                    "artifact_refs": artifact_refs,
                                    "workspace_exports": workspace_exports,
                                }
                            ),
                            {
                                "model": "qwen3.7-plus",
                                "provider": "qwen",
                                "input_tokens": 120,
                                "output_tokens": 60,
                                "total_tokens": 180,
                            },
                        )

                return _Agent()

            adapter = ProviderBackedSingleAgentAdapter(
                settings_factory=_settings_factory,
                preflight_fn=_ready_preflight,
                build_agent_factory=build_agent_factory,
                agentbay_toolkit_factory=toolkit_factory,
            )
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            result = await adapter.run(
                scenario=scenario,
                workspace_dir=workspace,
                prompt_text=build_prompt("incident_repair"),
                ledger=ledger,
                retry_policy=RetryPolicy(),
            )

            self.assertEqual(set(tool_names), {
                "start_execution_environment",
                "execute_command",
                "run_code",
                "read_text_file",
                "write_text_file",
                "list_files",
                "export_artifact",
                "inspect_artifact",
                "report_independent_validation",
                "close_execution_environment",
            })
            self.assertEqual(len(toolkit_instances), 1)
            self.assertEqual(toolkit_instances[0].close_all_calls, 1)
            self.assertEqual(toolkit_instances[0].start_calls, 1)
            self.assertEqual(toolkit_instances[0].workspace_source_dir, workspace)
            self.assertEqual(ledger.snapshot().qwen_model_calls, 1)
            self.assertEqual(ledger.snapshot().qwen_input_tokens, 120)
            self.assertEqual(ledger.snapshot().qwen_output_tokens, 60)
            self.assertTrue(result.cleanup_evidence)
            self.assertTrue(evaluate_attempt("incident_repair", workspace).passed)
            self.assertTrue(result.raw_outputs["fairness"]["single_agent_matches_full_union"])
            self.assertIn("already staged under /workspace", _single_agent_prompt(build_prompt("incident_repair"), scenario))

    async def test_society_provider_uses_team_composer_and_role_scoped_tool_union(self) -> None:
        scenario = load_public_scenario("incident_repair")
        toolkit_instances: list[_ToolkitRecorder] = []

        class _FakeProvider:
            async def propose(
                self,
                context: CompositionContext,
                catalog_snapshot: list[object],
                previous_issue_codes: list[str],
                attempt_number: int,
            ) -> TeamCompositionPlan:
                del context, catalog_snapshot, previous_issue_codes, attempt_number
                return TeamCompositionPlan(
                    task_summary="Provider-backed incident plan",
                    assignments=[
                        TeamAssignment(
                            id="repair",
                            agent_template_id="builder",
                            objective="Repair the public incident fixture and export the scored outputs.",
                            required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                            tool_grants=[ToolGrant(
                                capability="implementation",
                                tool_ids=[
                                    "start_execution_environment",
                                    "execute_command",
                                    "run_code",
                                    "read_text_file",
                                    "write_text_file",
                                    "list_files",
                                    "export_artifact",
                                    "close_execution_environment",
                                ],
                            )],
                        ),
                        TeamAssignment(
                            id="validate",
                            agent_template_id="test_engineer",
                            objective="Independently validate the repair outputs.",
                            required_capabilities=["test_execution", "sandbox_execution", "artifact_inspection"],
                            tool_grants=[ToolGrant(
                                capability="test_execution",
                                tool_ids=[
                                    "start_execution_environment",
                                    "execute_command",
                                    "inspect_artifact",
                                    "report_independent_validation",
                                    "close_execution_environment",
                                ],
                            )],
                            validates_assignment_ids=["repair"],
                            acceptance_checks=["artifact_diff_review", "independent_execution"],
                        ),
                    ],
                    work_graph=[
                        WorkNode(id="repair-node", assignment_id="repair"),
                        WorkNode(id="validate-node", assignment_id="validate", depends_on=["repair-node"]),
                    ],
                    selection_rationale="Repair and validation stay separate.",
                )

        class _FakeRuntime:
            def __init__(self, settings: Settings, artifact_root: Path, **kwargs: object) -> None:
                del settings, artifact_root
                self._agentbay_toolkit_factory = kwargs["agentbay_toolkit_factory"]
                self._event_sink = kwargs["event_sink"]

            def available_tool_ids(self) -> RuntimeAvailability:
                return RuntimeAvailability(
                    tool_ids=[
                        "start_execution_environment",
                        "execute_command",
                        "run_code",
                        "read_text_file",
                        "write_text_file",
                        "list_files",
                        "export_artifact",
                        "close_execution_environment",
                        "inspect_artifact",
                        "report_independent_validation",
                    ],
                    blockers=[],
                )

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
                    required_capabilities=list(required_capabilities),
                    available_tool_ids=self.available_tool_ids().tool_ids,
                    acceptance_requirements=list(acceptance_requirements),
                    unresolved_user_requirements=list(unresolved_user_requirements),
                )

            async def execute_plan(self, plan: TeamCompositionPlan, task_id: str, user_request: str) -> object:
                del plan, task_id, user_request
                repair_toolkit = self._agentbay_toolkit_factory(
                    task_id="repair-node",
                    role_key="builder",
                    allowed_remote_roots=["/workspace"],
                    artifact_root=runtime_export_root,
                    settings=_settings_factory(RetryPolicy()),
                    event_sink=lambda event: self._event_sink(event["event_type"], event),
                )
                validate_toolkit = self._agentbay_toolkit_factory(
                    task_id="validate-node",
                    role_key="test_engineer",
                    allowed_remote_roots=["/workspace"],
                    artifact_root=Path(tempfile.gettempdir()) / "validate-artifacts",
                    settings=_settings_factory(RetryPolicy()),
                    event_sink=lambda event: self._event_sink(event["event_type"], event),
                )
                toolkit_instances.extend([repair_toolkit._toolkit, validate_toolkit._toolkit])
                await repair_toolkit.start_execution_environment("repair-node", "repair")
                await validate_toolkit.start_execution_environment("validate-node", "validate")
                exports, _artifact_refs = _incident_exports(runtime_export_root)
                return SimpleNamespace(
                    graph_result=WorkGraphExecutionResult(
                        terminal_status=WorkGraphTerminalStatus.COMPLETED,
                        started_at=0.0,
                        finished_at=1.0,
                        duration_seconds=1.0,
                        nodes={
                            "repair-node": WorkNodeRuntimeRecord(
                                node_id="repair-node",
                                assignment_id="repair",
                                status=WorkNodeStatus.COMPLETED,
                                attempts=[WorkNodeAttemptRecord(
                                    attempt=1,
                                    status=WorkNodeStatus.COMPLETED,
                                    started_at=0.0,
                                    finished_at=1.0,
                                    duration_seconds=1.0,
                                )],
                            ),
                        },
                    ),
                    materialized_agents=[
                        MaterializedAgentSummary(id="assignment-repair", assignment_id="repair", template_id="builder", capabilities=["implementation"], can_vote=False),
                        MaterializedAgentSummary(id="assignment-validate", assignment_id="validate", template_id="test_engineer", capabilities=["test_execution"], can_vote=False),
                    ],
                    node_outputs={
                        "repair-node": {
                            "artifact_refs": [item["artifact_ref"] for item in exports],
                            "workspace_exports": exports,
                        }
                    },
                    availability_blockers=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            shutil.copytree(FIXTURES_ROOT / "incident_repair" / "dev", workspace)
            runtime_export_root = Path(temp_dir) / "runtime-exports"
            adapter = ProviderBackedSocietyAdapter(
                settings_factory=_settings_factory,
                preflight_fn=_ready_preflight,
                team_provider=_FakeProvider(),
                composition_runtime_factory=lambda settings, artifact_root, **kwargs: _FakeRuntime(settings, artifact_root, **kwargs),
                agentbay_toolkit_factory=lambda **kwargs: _ToolkitRecorder(**kwargs),
            )
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            result = await adapter.run(
                scenario=scenario,
                workspace_dir=workspace,
                prompt_text=build_prompt("incident_repair"),
                ledger=ledger,
                retry_policy=RetryPolicy(),
            )

            union = {
                tool_id
                for tool_ids in result.raw_outputs["granted_tool_ids_by_assignment"].values()
                for tool_id in tool_ids
            }
            self.assertIn("inspect_artifact", union)
            self.assertIn("write_text_file", union)
            self.assertTrue(result.raw_outputs["fairness"]["society_matches_full_union"])
            self.assertTrue(evaluate_attempt("incident_repair", workspace).passed)
            self.assertIsNotNone(result.graph_result)
            self.assertEqual(len(toolkit_instances), 2)
            self.assertEqual({toolkit.workspace_source_dir for toolkit in toolkit_instances}, {workspace})

    async def test_provider_preflight_refuses_before_external_calls(self) -> None:
        scenario = load_public_scenario("screenshot_to_product")
        called = False

        def build_agent_factory(identity: object, settings: object, **kwargs: object) -> object:
            nonlocal called
            called = True
            raise AssertionError("build_agent_factory should not be called")

        adapter = ProviderBackedSingleAgentAdapter(
            settings_factory=_settings_factory,
            preflight_fn=lambda settings: {
                **_ready_preflight(settings),
                "provider_services": {
                    **dict(_ready_preflight(settings)["provider_services"]),
                    "browser": {"ready": False},
                },
            },
            build_agent_factory=build_agent_factory,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            shutil.copytree(FIXTURES_ROOT / "screenshot_to_product" / "dev", workspace)
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            with self.assertRaises(ProviderAdapterRefusal):
                await adapter.run(
                    scenario=scenario,
                    workspace_dir=workspace,
                    prompt_text=build_prompt("screenshot_to_product"),
                    ledger=ledger,
                    retry_policy=RetryPolicy(),
                )
        self.assertFalse(called)

    async def test_provider_prompts_remove_reconstruction_instruction(self) -> None:
        scenario = load_public_scenario("incident_repair")
        prompt = build_prompt("incident_repair")

        single = _single_agent_prompt(prompt, scenario)
        society = _society_user_request(prompt, scenario)

        self.assertNotIn("Reconstruct only the necessary /workspace files", single)
        self.assertIn("already staged under /workspace", single)
        self.assertIn("already staged under /workspace", society)
        self.assertIn("executed-test count", single)
        self.assertIn("executed-test count", society)
        self.assertIn("explicit duplicate outcome", single)
        self.assertIn("explicit duplicate outcome", society)
        self.assertNotIn("reachable_vulnerability_closed", single)
        self.assertNotIn("reachable_vulnerability_closed", society)
        self.assertLess(single.index("Artifact semantics:"), single.index("Public benchmark prompt"))
        self.assertLess(society.index("Artifact semantics:"), society.index("Public benchmark prompt"))
        self.assertIn("executed-test count", society[:8_000])
        self.assertIn("minimum_remediated_version", society[:8_000])

    async def test_society_prompt_excludes_runtime_owned_cleanup_from_specialist_contract(self) -> None:
        scenario = load_public_scenario("incident_repair")
        prompt = build_prompt("incident_repair")

        society = _society_user_request(prompt, scenario)
        output_contract = society.split("Output contract:\n", 1)[1].split(
            "\n\nPublic fixture manifest:\n", 1
        )[0]
        decoded_contract = json.loads(output_contract)

        self.assertIn("cleanup/cleanup.json", prompt)
        self.assertNotIn("required_cleanup_markers", decoded_contract)
        self.assertNotIn("cleanup/cleanup.json", decoded_contract["required_paths"])
        self.assertNotIn("cleanup/cleanup.json", society.split("- Required paths:", 1)[1])
        self.assertIn("finalized by the runtime after the work graph", society)

    async def test_workspace_staging_is_traced_without_spending_subprocess_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(BudgetCaps(subprocess_seconds=1.0), Path(temp_dir) / "ledger.jsonl")
            traces = []
            records = await _record_staging_activity(
                [{
                    "event_type": "agentbay_workspace_stage_succeeded",
                    "role_key": "builder",
                    "source_root": str(Path(temp_dir) / "source"),
                    "remote_root": "/workspace",
                    "source_tree_hash": "a" * 64,
                    "staged_tree_hash": "a" * 64,
                    "file_count": 2,
                    "total_bytes": 128,
                    "duration_seconds": 9.5,
                    "started_at": 10.0,
                    "finished_at": 19.5,
                }],
                ledger,
                traces,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].category, "artifact_staging")
        self.assertEqual(traces[0].amount, 9.5)
        self.assertEqual(ledger.snapshot().subprocess_seconds, 0.0)

    async def test_run_attempt_provider_refuses_nonempty_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "occupied.txt").write_text("keep", encoding="utf-8")
            record = await run_attempt(
                scenario_id="incident_repair",
                benchmark_mode="single_agent",
                run_mode="development",
                output_dir=output_dir,
                adapter="provider",
            )
            self.assertEqual(record.status, "refused")
            self.assertIn("empty output directory", record.refusal_reasons[0])

    async def test_timeout_preserves_primary_failure_and_records_cleanup_failure(self) -> None:
        adapter = ProviderBackedSingleAgentAdapter(
            settings_factory=_settings_factory,
            preflight_fn=_ready_preflight,
            build_agent_factory=lambda *args, **kwargs: _SleepingAgent(),
            agentbay_toolkit_factory=lambda **kwargs: _FailingCloseToolkitRecorder(**kwargs),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("benchmarks.outcome_v4.runner.resolve_adapter", return_value=adapter):
                record = await run_attempt(
                    scenario_id="incident_repair",
                    benchmark_mode="single_agent",
                    run_mode="development",
                    output_dir=Path(temp_dir),
                    adapter="provider",
                    timeout_seconds=1.0,
                )
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.failure, {"error_type": "TimeoutError", "message": ""})
            self.assertEqual(record.raw_outputs["cleanup_failure"]["error_type"], "RuntimeError")
            self.assertEqual(record.raw_outputs["cleanup_failure"]["message"], "cleanup boom")
            self.assertEqual(record.cleanup_evidence, ["cleanup/cleanup.json", "cleanup/cleanup_failure.json"])

    async def test_workspace_exports_reject_outside_temp_file_and_persist_typed_failure(self) -> None:
        scenario = load_public_scenario("incident_repair")
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            shutil.copytree(FIXTURES_ROOT / "incident_repair" / "dev", workspace)
            outside_file = Path(temp_dir) / "outside-secret.txt"
            outside_file.write_text("do not copy", encoding="utf-8")

            def build_agent_factory(identity: object, settings: object, **kwargs: object) -> object:
                del identity, settings

                class _Agent:
                    async def arun(self, prompt: str) -> _FakeResponse:
                        del prompt
                        return _FakeResponse(
                            json.dumps(
                                {
                                    "summary": "bad export",
                                    "artifact_refs": [str(outside_file)],
                                    "workspace_exports": [{"workspace_relative_path": "reports/test_report.json", "artifact_ref": str(outside_file)}],
                                }
                            ),
                            {"model": "qwen3.7-plus", "provider": "qwen", "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        )

                return _Agent()

            adapter = ProviderBackedSingleAgentAdapter(
                settings_factory=_settings_factory,
                preflight_fn=_ready_preflight,
                build_agent_factory=build_agent_factory,
                agentbay_toolkit_factory=lambda **kwargs: _ToolkitRecorder(**kwargs),
            )
            with mock.patch("benchmarks.outcome_v4.runner.resolve_adapter", return_value=adapter):
                record = await run_attempt(
                    scenario_id=scenario.scenario_id,
                    benchmark_mode="single_agent",
                    run_mode="development",
                    output_dir=Path(temp_dir) / "attempt",
                    adapter="provider",
                )
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.failure["error_type"], "WorkspaceExportViolation")
            self.assertEqual(record.failure["code"], "workspace_export_outside_owned_roots")
            self.assertEqual(record.raw_outputs["typed_failure"]["code"], "workspace_export_outside_owned_roots")

    async def test_workspace_exports_reject_symlink_escape(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink not available")
        scenario = load_public_scenario("incident_repair")
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            shutil.copytree(FIXTURES_ROOT / "incident_repair" / "dev", workspace)
            export_root = workspace / "_provider_artifacts" / "single_agent"
            export_root.mkdir(parents=True, exist_ok=True)
            outside_file = Path(temp_dir) / "outside-secret.txt"
            outside_file.write_text("do not copy", encoding="utf-8")
            link = export_root / "escape.txt"
            try:
                os.symlink(outside_file, link)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            def build_agent_factory(identity: object, settings: object, **kwargs: object) -> object:
                del identity, settings

                class _Agent:
                    async def arun(self, prompt: str) -> _FakeResponse:
                        del prompt
                        return _FakeResponse(
                            json.dumps(
                                {
                                    "summary": "symlink export",
                                    "artifact_refs": [str(link)],
                                    "workspace_exports": [{"workspace_relative_path": "reports/test_report.json", "artifact_ref": str(link)}],
                                }
                            ),
                            {"model": "qwen3.7-plus", "provider": "qwen", "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        )

                return _Agent()

            adapter = ProviderBackedSingleAgentAdapter(
                settings_factory=_settings_factory,
                preflight_fn=_ready_preflight,
                build_agent_factory=build_agent_factory,
                agentbay_toolkit_factory=lambda **kwargs: _ToolkitRecorder(**kwargs),
            )
            with mock.patch("benchmarks.outcome_v4.runner.resolve_adapter", return_value=adapter):
                record = await run_attempt(
                    scenario_id=scenario.scenario_id,
                    benchmark_mode="single_agent",
                    run_mode="development",
                    output_dir=Path(temp_dir) / "attempt",
                    adapter="provider",
                )
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.failure["code"], "workspace_export_symlink_rejected")

    async def test_society_fairness_refuses_overgrant_before_execution(self) -> None:
        scenario = load_public_scenario("incident_repair")
        runtime_called = False
        bad_plan = TeamCompositionPlan(
            task_summary="bad fairness plan",
            assignments=[
                TeamAssignment(
                    id="repair",
                    agent_template_id="builder",
                    objective="repair",
                    required_capabilities=["implementation", "sandbox_execution", "artifact_export"],
                    tool_grants=[ToolGrant(
                        capability="implementation",
                        tool_ids=[
                            "start_execution_environment",
                            "execute_command",
                            "run_code",
                            "read_text_file",
                            "write_text_file",
                            "list_files",
                            "export_artifact",
                            "close_execution_environment",
                            "private_admin_tool",
                        ],
                    )],
                ),
                TeamAssignment(
                    id="validate",
                    agent_template_id="test_engineer",
                    objective="validate",
                    required_capabilities=["test_execution", "artifact_inspection"],
                    tool_grants=[ToolGrant(
                        capability="test_execution",
                        tool_ids=[
                            "start_execution_environment",
                            "execute_command",
                            "inspect_artifact",
                            "report_independent_validation",
                            "close_execution_environment",
                        ],
                    )],
                    validates_assignment_ids=["repair"],
                    acceptance_checks=["artifact_diff_review"],
                ),
            ],
            work_graph=[
                WorkNode(id="repair-node", assignment_id="repair"),
                WorkNode(id="validate-node", assignment_id="validate", depends_on=["repair-node"]),
            ],
            selection_rationale="bad",
        )

        class _FairnessRuntime:
            def __init__(self, settings: Settings, artifact_root: Path, **kwargs: object) -> None:
                del settings, artifact_root

            def available_tool_ids(self) -> RuntimeAvailability:
                return RuntimeAvailability(
                    tool_ids=[
                        "start_execution_environment",
                        "execute_command",
                        "run_code",
                        "read_text_file",
                        "write_text_file",
                        "list_files",
                        "export_artifact",
                        "inspect_artifact",
                        "report_independent_validation",
                        "close_execution_environment",
                    ],
                    blockers=[],
                )

            def build_composition_context(self, task_id: str, user_request: str, acceptance_requirements: list[str], *, required_capabilities: list[str] = (), unresolved_user_requirements: list[str] = ()) -> CompositionContext:
                return CompositionContext(
                    task_id=task_id,
                    task_summary=user_request,
                    user_request=user_request,
                    required_capabilities=list(required_capabilities),
                    available_tool_ids=self.available_tool_ids().tool_ids,
                    acceptance_requirements=list(acceptance_requirements),
                    unresolved_user_requirements=list(unresolved_user_requirements),
                )

            async def execute_plan(self, plan: TeamCompositionPlan, task_id: str, user_request: str) -> object:
                del plan, task_id, user_request
                nonlocal runtime_called
                runtime_called = True
                raise AssertionError("execute_plan should not run when fairness diverges")

        adapter = ProviderBackedSocietyAdapter(
            settings_factory=_settings_factory,
            preflight_fn=_ready_preflight,
            team_provider=object(),
            composition_runtime_factory=lambda settings, artifact_root, **kwargs: _FairnessRuntime(settings, artifact_root, **kwargs),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            shutil.copytree(FIXTURES_ROOT / "incident_repair" / "dev", workspace)
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            with (
                self.assertRaises(ProviderAdapterRefusal),
                mock.patch("benchmarks.outcome_v4.adapters.TeamComposer.compose", new=mock.AsyncMock(return_value=SimpleNamespace(plan=bad_plan))),
            ):
                await adapter.run(
                    scenario=scenario,
                    workspace_dir=workspace,
                    prompt_text=build_prompt("incident_repair"),
                    ledger=ledger,
                    retry_policy=RetryPolicy(),
                )
        self.assertFalse(runtime_called)

    async def test_cli_passes_adapter_selection(self) -> None:
        from benchmarks.outcome_v4 import cli

        args = cli.build_parser().parse_args([
            "run",
            "--scenario",
            "incident_repair",
            "--benchmark-mode",
            "single_agent",
            "--adapter",
            "provider",
            "--timeout-seconds",
            "300",
            "--output-dir",
            "tmp-out",
        ])
        with mock.patch("benchmarks.outcome_v4.cli.run_attempt", new=mock.AsyncMock(return_value=SimpleNamespace(status="success", attempt_id="a", scenario_id="incident_repair", benchmark_mode="single_agent")) ) as run_attempt_mock:
            exit_code = await cli._run_command(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(run_attempt_mock.await_args.kwargs["adapter"], "provider")
        self.assertEqual(run_attempt_mock.await_args.kwargs["timeout_seconds"], 300.0)

    async def test_cli_uses_development_provider_refusal_wording(self) -> None:
        from benchmarks.outcome_v4 import cli

        args = cli.build_parser().parse_args([
            "run",
            "--scenario",
            "incident_repair",
            "--benchmark-mode",
            "single_agent",
            "--adapter",
            "provider",
        ])
        refused = SimpleNamespace(
            status="refused",
            attempt_id="a",
            scenario_id="incident_repair",
            benchmark_mode="single_agent",
            refusal_reasons=["blocked"],
        )
        with (
            mock.patch("benchmarks.outcome_v4.cli.run_attempt", new=mock.AsyncMock(return_value=refused)),
            mock.patch("builtins.print") as print_mock,
        ):
            exit_code = await cli._run_command(args)
        self.assertEqual(exit_code, 2)
        self.assertIn("development/provider run refused", print_mock.call_args_list[0].args[0])

    async def test_single_agent_agno_function_schemas_preserve_exact_proxy_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            toolkit = _SchemaToolkitRecorder()
            wrapped_tools = _single_agent_tools(
                toolkit,
                "task-123",
                ledger,
                [],
                "screenshot_to_product",
            )

            expected_properties = {
                "start_execution_environment": ["purpose"],
                "execute_command": ["handle", "command_id", "arguments", "timeout_seconds"],
                "run_code": ["handle", "language", "code", "timeout_seconds"],
                "write_text_file": ["handle", "path", "content", "mode"],
                "export_artifact": ["handle", "path", "artifact_kind"],
                "inspect_artifact": ["artifact_ref"],
                "report_independent_validation": ["passed", "checks", "failures", "inspected_artifact_refs", "summary"],
                "close_execution_environment": ["handle"],
                "browser_render": ["handle", "entry_html_path"],
            }

            for tool in wrapped_tools:
                name = tool.__name__
                signature = inspect.signature(tool)
                schema = Function.from_callable(tool).parameters
                properties = list(schema.get("properties", {}).keys())
                self.assertNotIn("args", properties, msg=name)
                self.assertNotIn("kwargs", properties, msg=name)
                self.assertEqual(tool.__annotations__, getattr(tool.__wrapped__, "__annotations__", {}), msg=name)
                self.assertEqual(tool.__doc__, getattr(tool.__wrapped__, "__doc__", None), msg=name)
                self.assertTrue(inspect.iscoroutinefunction(tool), msg=name)
                self.assertEqual(signature, inspect.signature(tool.__wrapped__), msg=name)
                if name in expected_properties:
                    self.assertEqual(properties, expected_properties[name], msg=name)

            start_schema = Function.from_callable(wrapped_tools[0]).parameters
            self.assertEqual(start_schema["required"], ["purpose"])

    async def test_single_agent_proxy_tools_accept_keyword_calls_without_generic_args_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = AtomicBudgetLedger(BudgetCaps(), Path(temp_dir) / "ledger.jsonl")
            tool_traces = []
            toolkit = _SchemaToolkitRecorder()
            tools_by_name = {
                tool.__name__: tool
                for tool in _single_agent_tools(toolkit, "task-xyz", ledger, tool_traces, "screenshot_to_product")
            }

            start_result = await tools_by_name["start_execution_environment"](purpose="repair incident")
            execute_result = await tools_by_name["execute_command"](
                handle="handle-1",
                command_id="pytest_target",
                arguments={"target": "tests/test_ok.py"},
                timeout_seconds=5,
            )
            run_code_result = await tools_by_name["run_code"](
                handle="handle-1",
                language="python",
                code="print(1)",
                timeout_seconds=4,
            )
            write_result = await tools_by_name["write_text_file"](
                handle="handle-1",
                path="/workspace/report.txt",
                content="ok",
                mode="append",
            )
            export_result = await tools_by_name["export_artifact"](
                handle="handle-1",
                path="/workspace/report.txt",
                artifact_kind="report",
            )
            close_result = await tools_by_name["close_execution_environment"](handle="handle-1")
            browser_result = await tools_by_name["browser_render"](handle="handle-1")

            self.assertTrue(start_result["success"])
            self.assertTrue(execute_result["success"])
            self.assertTrue(run_code_result["success"])
            self.assertTrue(write_result["success"])
            self.assertTrue(export_result["success"])
            self.assertTrue(close_result["success"])
            self.assertTrue(browser_result["success"])
            self.assertEqual(
                toolkit.calls,
                [
                    ("start_execution_environment", {"task_id": "task-xyz", "purpose": "repair incident"}),
                    (
                        "execute_command",
                        {
                            "handle": "handle-1",
                            "command_id": "pytest_target",
                            "arguments": {"target": "tests/test_ok.py"},
                            "timeout_seconds": 5,
                        },
                    ),
                    (
                        "run_code",
                        {
                            "handle": "handle-1",
                            "language": "python",
                            "code": "print(1)",
                            "timeout_seconds": 4,
                        },
                    ),
                    (
                        "write_text_file",
                        {
                            "handle": "handle-1",
                            "path": "/workspace/report.txt",
                            "content": "ok",
                            "mode": "append",
                        },
                    ),
                    (
                        "export_artifact",
                        {
                            "handle": "handle-1",
                            "path": "/workspace/report.txt",
                            "artifact_kind": "report",
                        },
                    ),
                    ("close_execution_environment", {"handle": "handle-1"}),
                    (
                        "browser_render",
                        {
                            "handle": "handle-1",
                            "entry_html_path": "/workspace/app/dist/index.html",
                        },
                    ),
                ],
            )
            self.assertEqual([trace.tool_id for trace in tool_traces], [
                "start_execution_environment",
                "execute_command",
                "run_code",
                "write_text_file",
                "export_artifact",
                "close_execution_environment",
                "browser_render",
            ])


if __name__ == "__main__":
    unittest.main()
