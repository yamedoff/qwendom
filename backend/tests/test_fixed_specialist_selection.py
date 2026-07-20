"""Acceptance tests for leader-selected immutable specialist bundles."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.capability_registry import (
    get_fixed_specialist_template,
    list_fixed_specialist_templates,
    resolve_specialist_bundle,
)
from society.composition_runtime import _build_assignment_identity
from society.schemas.team_composition import validate_team_composition_plan
from society.specialist_selection import (
    AgnoFixedSpecialistSelectionProvider,
    FixedSpecialistCoordinator,
    InvokeSpecialistCall,
    SelectSpecialistsCall,
    SpecialistAssignmentSelection,
    SpecialistSelectionError,
    fixed_template_validation_registry,
)
from society.tools.specialists import specialist_discovery_instructions


def _all_fixed_tools() -> list[str]:
    return sorted({
        tool_id
        for template in list_fixed_specialist_templates().values()
        for tool_id in template.tool_ids
    })


def _builder_then_validator() -> SelectSpecialistsCall:
    return SelectSpecialistsCall(
        selection_rationale="A Builder implements the repair and a separate Test Engineer validates it.",
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
                objective="Validate the exported repair independently.",
                depends_on=["build"],
                owned_artifacts=[],
                acceptance_requirements=["unit_tests"],
            ),
        ],
    )


def test_leader_provider_receives_required_artifact_ownership_as_first_class_input() -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        async def arun(self, prompt: str) -> SelectSpecialistsCall:
            captured["prompt"] = prompt
            return _builder_then_validator()

    def agent_factory(**kwargs: object) -> FakeAgent:
        captured["instructions"] = kwargs["instructions"]
        captured["structured_outputs"] = kwargs["structured_outputs"]
        captured["use_json_mode"] = kwargs["use_json_mode"]
        return FakeAgent()

    provider = AgnoFixedSpecialistSelectionProvider(agent_factory=agent_factory)
    result = asyncio.run(
        provider.propose(
            task_summary="Repair a repository",
            user_request="Produce a tested repair",
            acceptance_requirements=["unit_tests"],
            catalog=FixedSpecialistCoordinator(_all_fixed_tools()).list_specialists(),
            required_artifacts=["src/calc.py"],
            prior_blockers=[{"code": "required_artifacts_unowned"}],
            attempt_number=2,
        )
    )

    assert result == _builder_then_validator()
    assert 'Required artifact ownership: ["src/calc.py"]' in str(captured["prompt"])
    assert "must contain every required artifact exactly once" in " ".join(captured["instructions"])
    assert "Prefer one capable producer" in " ".join(captured["instructions"])
    assert "omit media artifacts and record a missing-system-capability blocker" in " ".join(captured["instructions"])
    assert captured["structured_outputs"] is False
    assert captured["use_json_mode"] is True


def test_fixed_catalog_resolves_exact_role_scoped_skills_without_leakage() -> None:
    builder = resolve_specialist_bundle("builder")
    validator = resolve_specialist_bundle("test_engineer")

    assert [(skill.skill_id, skill.version) for skill in builder.skills] == [
        ("repository_implementation", "2")
    ]
    assert builder.skills[0].sha256 == "57b369c677b252a17144b9c74d34f95773db6505bbb81a7171b9c32399a1c090"
    assert "List and read back every owned path" in builder.skills[0].content
    assert "do not use `run_code` for those imports" in builder.skills[0].content
    assert [(skill.skill_id, skill.version) for skill in validator.skills] == [
        ("independent_validation", "1")
    ]
    assert "Independent validation" not in builder.skills[0].content
    assert "Repository implementation" not in validator.skills[0].content
    assert builder.tool_bundle_hash != validator.tool_bundle_hash


def test_agno_specialist_discovery_skill_describes_available_media_specialists() -> None:
    """Readiness guidance must expose discovery without granting media tools."""

    instructions = specialist_discovery_instructions()
    catalog = FixedSpecialistCoordinator(_all_fixed_tools()).list_specialists()
    image_creator = next(entry for entry in catalog if entry.template_id == "image_creator")

    assert len(instructions) == 1
    assert "missing_system_capability" in instructions[0]
    assert "Only the elected leader may select" in instructions[0]
    assert image_creator.available is True
    assert image_creator.description == "Generates, inspects, and publishes durable image artifacts for review."
    assert set(image_creator.tool_ids) == {"generate_images", "inspect_image", "publish_image"}

    image_bundle = resolve_specialist_bundle("image_creator")
    assert [(skill.skill_id, skill.version) for skill in image_bundle.skills] == [("image_generation", "1")]
    assert "generate_images" in image_bundle.skills[0].content


def test_image_creator_selection_adds_its_required_provenance_checks_to_validator() -> None:
    """The leader selects roles; immutable policy supplies validation checks."""

    selection = SelectSpecialistsCall(
        selection_rationale="Generate one image and validate it independently.",
        assignments=[
            SpecialistAssignmentSelection(
                assignment_id="image",
                template_id="image_creator",
                objective="Generate and publish one image.",
                depends_on=[],
                owned_artifacts=["launch.png"],
                acceptance_requirements=["image is published"],
            ),
            SpecialistAssignmentSelection(
                assignment_id="validate",
                template_id="test_engineer",
                objective="Validate the generated image.",
                depends_on=["image"],
                owned_artifacts=[],
                acceptance_requirements=["image is published"],
            ),
        ],
    )

    resolved = asyncio.run(
        FixedSpecialistCoordinator(_all_fixed_tools()).select_specialists(
            selection,
            task_summary="Generate one launch image.",
        )
    )
    validator = next(item for item in resolved.plan.assignments if item.id == "validate")
    assert {"independent_validation", "artifact_collection", "artifact_provenance"}.issubset(
        validator.acceptance_checks
    )


def test_frontend_browser_delivery_skill_is_versioned_and_keeps_exact_template_tools() -> None:
    frontend = resolve_specialist_bundle("frontend_engineer")
    template = get_fixed_specialist_template("frontend_engineer")

    assert template is not None
    assert template.version == "2"
    assert [(skill.skill_id, skill.version) for skill in frontend.skills] == [("frontend_browser_delivery", "1")]
    assert frontend.skills[0].sha256 == "f893372cee8bce3ab13195720f73cf76b52e11a04221b578b48070233e28dafb"
    assert "/workspace/app/dist/index.html" in frontend.skills[0].content
    assert "Call `browser_render` exactly once" in frontend.skills[0].content
    assert "Do not attempt image or video generation unless those tools are granted" in frontend.skills[0].content
    assert set(template.tool_ids) == {
        "start_execution_environment", "execute_command", "run_code", "read_text_file", "write_text_file",
        "list_files", "browser_render", "export_artifact", "close_execution_environment",
    }


def test_frontend_browser_delivery_skill_hash_drift_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import society.capability_registry as registry

    path = tmp_path / "frontend_browser_delivery" / "1" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("drifted", encoding="utf-8")
    monkeypatch.setattr(registry, "_SPECIALIST_SKILL_ROOT", tmp_path)

    with pytest.raises(ValueError, match="specialist_skill_hash_mismatch:frontend_browser_delivery@1"):
        resolve_specialist_bundle("frontend_engineer")


@pytest.mark.parametrize("forbidden_field", ["tool_ids", "tool_grants", "skill_ids", "skills", "resource_policy"])
def test_leader_assignment_schema_rejects_policy_override_fields(forbidden_field: str) -> None:
    payload = {
        "assignment_id": "build",
        "template_id": "builder",
        "objective": "Implement the repair.",
        forbidden_field: ["untrusted"] if forbidden_field != "resource_policy" else {"network_policy": "unrestricted"},
    }

    with pytest.raises(ValidationError):
        SpecialistAssignmentSelection.model_validate(payload)


def test_unknown_template_fails_before_any_external_call() -> None:
    events: list[tuple[str, dict]] = []
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools(), lambda event, payload: events.append((event, dict(payload))))
    call = SelectSpecialistsCall(
        selection_rationale="Use an unknown template.",
        assignments=[SpecialistAssignmentSelection(
            assignment_id="unknown",
            template_id="invented_role",
            objective="Do work.",
            depends_on=[],
            owned_artifacts=[],
            acceptance_requirements=[],
        )],
    )

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(call, task_summary="Repair a repository"))

    assert [blocker.code for blocker in exc_info.value.blockers] == ["unknown_specialist_template"]
    assert [event for event, _payload in events] == [
        "specialist_selection_proposed",
        "specialist_selection_rejected",
    ]


def test_validation_only_specialist_cannot_own_producer_artifacts() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())
    call = SelectSpecialistsCall(
        selection_rationale="Invalidly ask the validator to produce a report.",
        assignments=[
            SpecialistAssignmentSelection(
                assignment_id="validate",
                template_id="test_engineer",
                objective="Validate and produce a report.",
                depends_on=[],
                owned_artifacts=["reports/test_report.json"],
                acceptance_requirements=["unit_tests"],
            )
        ],
    )

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(call, task_summary="Validate a repository"))

    assert [blocker.code for blocker in exc_info.value.blockers] == [
        "specialist_cannot_produce_artifacts"
    ]


def test_leader_schema_requires_explicit_dependency_ownership_and_acceptance_lists() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SpecialistAssignmentSelection.model_validate({
            "assignment_id": "build",
            "template_id": "builder",
            "objective": "Implement a repair.",
        })

    missing = {tuple(error["loc"]) for error in exc_info.value.errors() if error["type"] == "missing"}
    assert missing == {("depends_on",), ("owned_artifacts",), ("acceptance_requirements",)}


def test_missing_required_tool_fails_closed_without_shrinking_bundle() -> None:
    coordinator = FixedSpecialistCoordinator([
        tool_id for tool_id in _all_fixed_tools() if tool_id != "export_artifact"
    ])

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(_builder_then_validator(), task_summary="Repair a repository"))

    assert any(blocker.code == "specialist_tools_unavailable" for blocker in exc_info.value.blockers)
    assert "export_artifact" in get_fixed_specialist_template("builder").tool_ids


def test_required_artifact_without_owner_is_rejected_before_execution() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(
            _builder_then_validator(),
            task_summary="Repair a repository",
            required_artifacts=["src/calc.py", "reports/missing.json"],
        ))

    assert exc_info.value.blockers[0].code == "required_artifacts_unowned"
    assert "reports/missing.json" in exc_info.value.blockers[0].message


def test_unlisted_artifact_ownership_is_rejected_before_execution() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())
    selection = _builder_then_validator()
    selection.assignments[0].owned_artifacts.append("cleanup/cleanup.json")

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(
            selection,
            task_summary="Repair a repository",
            required_artifacts=["src/calc.py"],
        ))

    assert any(blocker.code == "unexpected_artifact_ownership" for blocker in exc_info.value.blockers)


def test_acceptance_cannot_require_path_outside_exact_artifact_contract() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())
    selection = _builder_then_validator()
    selection.assignments[1].acceptance_requirements.append("Verify cleanup/cleanup.json exists")

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(
            selection,
            task_summary="Repair a repository",
            required_artifacts=["src/calc.py"],
        ))

    assert any(blocker.code == "acceptance_artifact_outside_contract" for blocker in exc_info.value.blockers)


def test_builder_validator_selection_persists_replay_and_validation_metadata() -> None:
    events: list[tuple[str, dict]] = []
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools(), lambda event, payload: events.append((event, dict(payload))))

    resolved = asyncio.run(coordinator.select_specialists(
        _builder_then_validator(),
        task_summary="Repair a repository",
        required_acceptance_requirements=["artifact_diff_review", "rollback_constraints"],
    ))

    builder, validator = resolved.assignments
    assert builder.template_version == "2"
    assert builder.skill_ids == ("repository_implementation",)
    assert len(builder.tool_bundle_hash) == 64
    assert validator.skill_ids == ("independent_validation",)
    assert validator.depends_on == ("build",)
    assert validator.validates_assignment_ids == ("build",)
    assert "independent_validation" in validator.acceptance_requirements
    assert "artifact_diff_review" in validator.acceptance_requirements
    assert "rollback_constraints" in validator.acceptance_requirements
    assert resolved.plan.work_graph[1].depends_on == ["build"]
    assert events[-1][0] == "specialist_selection_accepted"
    assert resolved.plan.assignments[0].resolved_skills[0].skill_id == "repository_implementation"
    assert resolved.plan.assignments[1].resolved_skills[0].skill_id == "independent_validation"

    materialized_builder = _build_assignment_identity(resolved.plan.assignments[0])
    materialized_validator = _build_assignment_identity(resolved.plan.assignments[1])
    assert "# Repository implementation" in materialized_builder.skill_instructions[0]
    assert "A later successful execution is required" in materialized_builder.skill_instructions[0]
    assert "# Independent validation" in materialized_validator.skill_instructions[0]
    assert materialized_builder.summary.skills[0]["sha256"] == builder.skill_hashes[0]
    assert materialized_validator.summary.skills[0]["sha256"] == validator.skill_hashes[0]

    validate_team_composition_plan(
        resolved.plan,
        fixed_template_validation_registry(),
        available_tool_ids=_all_fixed_tools(),
        available_agent_template_ids={"builder", "test_engineer"},
    )


def test_browser_render_team_materializes_exact_frontend_bundle_and_independent_validator() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())
    selection = SelectSpecialistsCall(
        selection_rationale="Build the site, implement the browser UI, then validate both exports independently.",
        assignments=[
            SpecialistAssignmentSelection(
                assignment_id="build", template_id="builder", objective="Implement the supporting code.",
                depends_on=[], owned_artifacts=["src/server.py"], acceptance_requirements=["unit_tests"],
            ),
            SpecialistAssignmentSelection(
                assignment_id="frontend", template_id="frontend_engineer", objective="Implement and render the website.",
                depends_on=["build"], owned_artifacts=["web/index.html"], acceptance_requirements=["browser_render"],
            ),
            SpecialistAssignmentSelection(
                assignment_id="validate", template_id="test_engineer", objective="Independently validate the exports.",
                depends_on=["build", "frontend"], owned_artifacts=[], acceptance_requirements=["unit_tests", "browser_render"],
            ),
        ],
    )

    resolved = asyncio.run(coordinator.select_specialists(
        selection,
        task_summary="Build and browser-render a website.",
        required_artifacts=["src/server.py", "web/index.html"],
    ))

    assignments = {assignment.agent_template_id: assignment for assignment in resolved.plan.assignments}
    frontend = assignments["frontend_engineer"]
    validator = assignments["test_engineer"]
    frontend_template = get_fixed_specialist_template("frontend_engineer")
    assert frontend_template is not None
    assert set(frontend.tool_grants[0].tool_ids) == set(frontend_template.tool_ids)
    assert "list_files" in frontend.tool_grants[0].tool_ids
    assert validator.owned_paths == []
    assert validator.validates_assignment_ids == ["build", "frontend"]
    materialized_frontend = _build_assignment_identity(frontend)
    assert materialized_frontend.summary.tool_bundle_hash == frontend.tool_bundle_hash
    assert "Call `browser_render` exactly once" in materialized_frontend.skill_instructions[0]


def test_frontend_media_placeholder_ownership_is_rejected_but_browser_screenshots_and_retry_pass() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())
    media_attempt = SelectSpecialistsCall(
        selection_rationale="Build the site and include requested media.",
        assignments=[
            SpecialistAssignmentSelection(
                assignment_id="frontend", template_id="frontend_engineer", objective="Build and render the site.",
                depends_on=[],
                owned_artifacts=[
                    "web/index.html", "screenshots/desktop.png", "screenshots/mobile.png",
                    "assets/hero_image_attempt.png", "assets/promo_video_attempt.mp4",
                ],
                acceptance_requirements=["browser_render"],
            ),
        ],
    )

    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.select_specialists(
            media_attempt,
            task_summary="Build an Algiers website with browser evidence.",
            required_artifacts=[
                "web/index.html", "screenshots/desktop.png", "screenshots/mobile.png",
                "assets/hero_image_attempt.png", "assets/promo_video_attempt.mp4",
            ],
        ))

    media_blockers = [blocker for blocker in exc_info.value.blockers if blocker.code == "specialist_media_artifact_capability_unavailable"]
    assert len(media_blockers) == 2
    assert all("missing_system_capability blocker" in blocker.message for blocker in media_blockers)

    retry = SelectSpecialistsCall(
        selection_rationale="Deliver the available website and browser evidence; media is unavailable.",
        assignments=[
            SpecialistAssignmentSelection(
                assignment_id="frontend", template_id="frontend_engineer", objective="Build and render the site.",
                depends_on=[], owned_artifacts=["web/index.html", "screenshots/desktop.png", "screenshots/mobile.png"],
                acceptance_requirements=["browser_render"],
            ),
            SpecialistAssignmentSelection(
                assignment_id="validate", template_id="test_engineer", objective="Independently validate browser evidence.",
                depends_on=["frontend"], owned_artifacts=[], acceptance_requirements=["browser_render"],
            ),
        ],
    )
    resolved = asyncio.run(coordinator.select_specialists(
        retry,
        task_summary="Build an Algiers website with browser evidence and report unavailable media.",
        required_artifacts=["web/index.html", "screenshots/desktop.png", "screenshots/mobile.png"],
    ))
    assert resolved.plan.assignments[0].agent_template_id == "frontend_engineer"
    assert resolved.plan.assignments[1].validates_assignment_ids == ["frontend"]


def test_invoke_only_accepts_assignments_from_accepted_selection() -> None:
    coordinator = FixedSpecialistCoordinator(_all_fixed_tools())
    asyncio.run(coordinator.select_specialists(_builder_then_validator(), task_summary="Repair a repository"))

    invoked = asyncio.run(coordinator.invoke_specialist(InvokeSpecialistCall(assignment_id="build")))
    assert invoked.assignment_id == "build"
    with pytest.raises(SpecialistSelectionError) as dependency_exc:
        asyncio.run(coordinator.invoke_specialist(InvokeSpecialistCall(assignment_id="validate")))
    assert dependency_exc.value.blockers[0].code == "specialist_dependencies_unmet"
    validator = asyncio.run(coordinator.invoke_specialist(
        InvokeSpecialistCall(assignment_id="validate"),
        satisfied_dependency_ids=["build"],
    ))
    assert validator.assignment_id == "validate"
    with pytest.raises(SpecialistSelectionError) as exc_info:
        asyncio.run(coordinator.invoke_specialist(InvokeSpecialistCall(assignment_id="not-selected")))
    assert exc_info.value.blockers[0].code == "unknown_specialist_assignment"


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (lambda _path: None, "specialist_skill_missing"),
        (lambda path: path.write_text("drifted", encoding="utf-8"), "specialist_skill_hash_mismatch"),
    ],
)
def test_missing_or_drifted_skill_fails_closed_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator,
    expected_error: str,
) -> None:
    import society.capability_registry as registry

    monkeypatch.setattr(registry, "_SPECIALIST_SKILL_ROOT", tmp_path)
    if expected_error == "specialist_skill_hash_mismatch":
        path = tmp_path / "repository_implementation" / "2" / "SKILL.md"
        path.parent.mkdir(parents=True)
        mutator(path)

    with pytest.raises(ValueError, match=expected_error):
        resolve_specialist_bundle("builder")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "selection_rationale": "Duplicate IDs.",
            "assignments": [
                {"assignment_id": "same", "template_id": "builder", "objective": "Build."},
                {"assignment_id": "same", "template_id": "test_engineer", "objective": "Test."},
            ],
        },
        {
            "selection_rationale": "Unknown dependency.",
            "assignments": [
                {"assignment_id": "test", "template_id": "test_engineer", "objective": "Test.", "depends_on": ["missing"]},
            ],
        },
        {
            "selection_rationale": "Cycle.",
            "assignments": [
                {"assignment_id": "a", "template_id": "builder", "objective": "A.", "depends_on": ["b"]},
                {"assignment_id": "b", "template_id": "test_engineer", "objective": "B.", "depends_on": ["a"]},
            ],
        },
        {
            "selection_rationale": "Conflicting ownership.",
            "assignments": [
                {"assignment_id": "a", "template_id": "builder", "objective": "A.", "owned_artifacts": ["same.py"]},
                {"assignment_id": "b", "template_id": "builder", "objective": "B.", "owned_artifacts": ["same.py"]},
            ],
        },
    ],
)
def test_invalid_work_graph_or_ownership_is_schema_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        SelectSpecialistsCall.model_validate(payload)
