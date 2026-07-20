"""Capability registry, reassignment, and voter-separation tests."""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society import capability_registry
from society.capability_registry import (
    AGENTBAY_TOOL_ID_ALIASES,
    CORE_ROLE_CAPABILITIES,
    RoleCapability,
    canonical_tool_id,
    default_required_capabilities,
    find_capable_team_member,
    get_role_capabilities,
    register_specialist_template,
    resolve_role_key,
)
from society.models import AgentProfile, SocietyAgent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator
from society.schemas.governance import VoteDecision


def _agent(agent_id: str, role: str = "Test role", *, parent_id: str | None = None) -> SocietyAgent:
    return SocietyAgent(
        id=agent_id, name=agent_id, role=role, skills=[role], parent_id=parent_id,
        profile=AgentProfile(),
    )


AGENTBAY_TOOLKIT_NAMES = {
    "start_execution_environment",
    "execute_command",
    "run_code",
    "read_text_file",
    "write_text_file",
    "list_files",
    "export_artifact",
    "close_execution_environment",
}
IMAGE_TOOLKIT_NAMES = {"generate_images", "inspect_image", "publish_image"}
VIDEO_TOOLKIT_NAMES = {"submit_text_to_video", "get_video_job", "cancel_video_job", "collect_video", "inspect_video"}


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        """Snapshot mutable extension state so registration tests stay isolated."""

        self._specialist_templates = deepcopy(capability_registry._SPECIALIST_TEMPLATES)

    def tearDown(self) -> None:
        """Restore the canonical registry after each extension test."""

        capability_registry._SPECIALIST_TEMPLATES.clear()
        capability_registry._SPECIALIST_TEMPLATES.update(self._specialist_templates)

    def test_accepted_role_catalog_is_registered_with_distinct_policy(self) -> None:
        expected = {
            "coordinator": (False, False, False, {"capability_search", "team_plan", "work_graph_plan", "typed_blocker"}, set()),
            "architect": (False, True, True, {"repository_read", "dependency_graph", "architecture_artifact"}, {"decompose_task", "risk_assessment"}),
            "researcher": (False, True, True, {"context7_lookup", "approved_web_lookup", "citation_lookup"}, {"context7_lookup"}),
            "builder": (True, True, False, {"repository_read", "repository_write", "start_execution_environment", "execute_command", "run_code", "read_text_file", "write_text_file", "list_files", "export_artifact", "close_execution_environment"}, {"execute_notes_demo"}),
            "frontend_engineer": (True, False, False, {"repository_read", "repository_write", "start_execution_environment", "execute_command", "run_code", "read_text_file", "write_text_file", "browser_render", "browser_test", "export_artifact", "close_execution_environment"}, set()),
            "test_engineer": (False, False, True, {"repository_read", "start_execution_environment", "execute_command", "run_code", "read_text_file", "list_files", "browser_render", "inspect_artifact", "report_independent_validation", "close_execution_environment"}, set()),
            "image_creator": (False, False, False, {"generate_images", "generate_image", "inspect_image", "publish_image"}, set()),
            "video_producer": (False, False, False, {"submit_text_to_video", "get_video_job", "cancel_video_job", "collect_video", "inspect_video"}, set()),
            "data_analyst": (False, False, False, {"repository_read", "start_execution_environment", "run_code", "read_text_file", "list_files", "data_file_read", "table_artifact", "chart_artifact", "export_artifact", "close_execution_environment"}, set()),
            "critic": (False, True, True, {"read_artifacts", "review_reports", "rubric_check"}, {"risk_assessment", "read_artifacts"}),
        }

        for role_key, (can_write, can_vote, can_accept, accepted_tools, legacy_tools) in expected.items():
            registration = get_role_capabilities(role_key)
            self.assertIsNotNone(registration, role_key)
            self.assertEqual(registration.can_write_product_files, can_write)
            self.assertEqual(registration.can_vote, can_vote)
            self.assertEqual(registration.can_accept_artifacts, can_accept)
            self.assertTrue(accepted_tools.issubset(set(registration.allowed_tools)))
            self.assertTrue(legacy_tools.issubset(set(registration.allowed_tools)))

    def test_legacy_core_capabilities_are_preserved_as_subsets(self) -> None:
        expected = {
            "architect": {"decomposition", "architecture", "tradeoffs", "system_design"},
            "researcher": {"evidence", "assumption_testing", "context_gathering", "context7_research"},
            "builder": {"prototyping", "integration", "delivery", "code_execution"},
            "critic": {"risk_analysis", "quality_gates", "counterarguments", "validation"},
        }

        for role_key, legacy_capabilities in expected.items():
            registration = get_role_capabilities(role_key)
            self.assertTrue(legacy_capabilities.issubset(set(registration.capabilities)))
            self.assertEqual(len(registration.capabilities), len(set(registration.capabilities)))
            self.assertEqual(len(registration.allowed_tools), len(set(registration.allowed_tools)))

    def test_agentbay_required_tools_use_only_actual_toolkit_names(self) -> None:
        for role_key in ("builder", "frontend_engineer", "test_engineer", "data_analyst"):
            registration = get_role_capabilities(role_key)
            required_agentbay_tools = {
                tool_id for tool_id in registration.required_tools if tool_id in AGENTBAY_TOOLKIT_NAMES
            }
            self.assertEqual(required_agentbay_tools, set(registration.required_tools) & AGENTBAY_TOOLKIT_NAMES)
            self.assertTrue(required_agentbay_tools)
            self.assertTrue(required_agentbay_tools.issubset(AGENTBAY_TOOLKIT_NAMES))

    def test_image_and_video_required_tools_use_actual_toolkit_names(self) -> None:
        image_registration = get_role_capabilities("image_creator")
        self.assertEqual(set(image_registration.required_tools), IMAGE_TOOLKIT_NAMES)
        self.assertEqual(set(image_registration.required_tools), set(image_registration.required_tools) & IMAGE_TOOLKIT_NAMES)

        video_registration = get_role_capabilities("video_producer")
        self.assertTrue(set(video_registration.required_tools).issubset(VIDEO_TOOLKIT_NAMES))

    def test_generate_image_legacy_alias_canonicalizes_to_generate_images(self) -> None:
        self.assertEqual(canonical_tool_id("generate_image"), "generate_images")

    def test_legacy_agentbay_aliases_canonicalize_to_actual_tool_names(self) -> None:
        self.assertEqual(
            {alias: canonical_tool_id(alias) for alias in AGENTBAY_TOOL_ID_ALIASES},
            dict(AGENTBAY_TOOL_ID_ALIASES),
        )

    def test_test_validation_engineer_alias_preserves_accepted_policy(self) -> None:
        canonical = get_role_capabilities("test_engineer")
        alias = get_role_capabilities("test_validation_engineer")
        self.assertEqual(alias.model_dump(), canonical.model_dump())
        self.assertEqual(default_required_capabilities("test_validation_engineer"), ["test_execution"])

    def test_new_specialist_template_requires_no_orchestrator_change(self) -> None:
        register_specialist_template(RoleCapability(
            role="data_reviewer", capabilities=["data_validation"], can_vote=False,
        ))
        self.assertEqual(get_role_capabilities("data_reviewer").capabilities, ["data_validation"])

    def test_alias_registration_updates_canonical_template_without_shadow_entry(self) -> None:
        register_specialist_template(RoleCapability(
            role="test_validation_engineer",
            capabilities=["test_execution", "artifact_inspection"],
            can_accept_artifacts=True,
            allowed_tools=["inspect_artifact"],
        ))
        canonical = get_role_capabilities("test_engineer")
        alias = get_role_capabilities("test_validation_engineer")
        self.assertEqual(canonical.role, "test_engineer")
        self.assertEqual(alias.role, "test_engineer")
        self.assertEqual(canonical.allowed_tools, ["inspect_artifact"])

    def test_capable_member_selected_without_arbitrary_fallback(self) -> None:
        agents = {"builder": _agent("builder"), "researcher": _agent("researcher")}
        self.assertEqual(
            find_capable_team_member(["builder", "researcher"], agents, ["evidence_gathering"], {"builder"}),
            "researcher",
        )
        self.assertIsNone(
            find_capable_team_member(["researcher"], agents, ["implementation"], set()),
        )

    def test_core_role_registry_contains_accepted_governance_roles(self) -> None:
        self.assertEqual(set(CORE_ROLE_CAPABILITIES), {"coordinator", "architect", "researcher", "builder", "critic"})

    def test_resolve_role_key_handles_test_validation_punctuation(self) -> None:
        agent = _agent("validator", "Test/Validation Engineer", parent_id="architect")
        self.assertEqual(resolve_role_key(agent), "test_validation_engineer")


class VoterSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_validator_executes_without_vote(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch.settings = SimpleNamespace(llm_enabled=False)
        orch.agents = {
            "architect": _agent("architect", "Systems Architect"),
            "validator": _agent("validator", "Test Validation Engineer", parent_id="architect"),
        }
        task = TaskRun(prompt="verify this deliverable", status="running", final_answer="done")
        team = Team(
            task_id=task.id, member_ids=["architect", "validator"], voter_ids=["architect"],
            leader_id="architect", status="active",
        )
        state = {
            "child_agents": [], "acceptance_checks": [{"check": "proof", "passed": True}],
            "failed_checks": [], "acceptance_evidence": {"terminal_status": "complete"},
        }
        orch._state = lambda _task_id: state
        orch._emit = lambda *args, **kwargs: None
        orch._register_artifact = lambda *args, **kwargs: None

        await orch._run_independent_validator(task, team)

        validator_id = state["independent_validation"]["validator_id"]
        self.assertNotIn(validator_id, team.voter_ids)
        self.assertIn(validator_id, team.member_ids)
        self.assertTrue(state["independent_validation"]["passed"])

    async def test_nonvoting_specialist_is_not_asked_to_vote(self) -> None:
        orch = SocietyOrchestrator.__new__(SocietyOrchestrator)
        orch.settings = SimpleNamespace(readiness_concurrency=3, native_voting_enabled=True)
        orch.agents = {
            "architect": _agent("architect"),
            "critic": _agent("critic"),
            "child": _agent("child", "Test Engineer", parent_id="architect"),
        }
        state = {"proposals": {}, "ballots": []}
        orch._state = lambda _task_id: state
        called: list[str] = []

        async def fake_isolated(**kwargs):
            actor = kwargs["actor_identity"].id
            called.append(actor)
            return VoteDecision(choice="architect", reason="ok", confidence=1.0)

        orch._run_governance_tool_isolated = fake_isolated
        task = TaskRun(prompt="test task", status="running")
        team = Team(
            task_id=task.id,
            member_ids=["architect", "critic", "child"],
            voter_ids=["architect", "critic"],
        )
        await orch._collect_votes_concurrent(task, team, ["architect", "critic"])
        self.assertEqual(called, ["architect", "critic"])

    def test_legacy_team_falls_back_to_members(self) -> None:
        team = Team(task_id="t", member_ids=["a", "b"])
        self.assertEqual(SocietyOrchestrator._voter_ids(team), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
