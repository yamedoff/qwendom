from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NetworkPolicy = Literal["disabled", "allowlist", "unrestricted"]

AGENTBAY_TOOL_ID_ALIASES = MappingProxyType({
    "agentbay_execute": "execute_command",
    "agentbay_run_code": "run_code",
    "agentbay_write_file": "write_text_file",
    "agentbay_list_files": "list_files",
    "agentbay_export_artifact": "export_artifact",
    "browser_test": "browser_render",
    "generate_image": "generate_images",
})
"""Immutable compatibility aliases for persisted AgentBay tool identifiers.

The runtime now exposes the underlying Agno function names directly. Older
plans may still persist the previous ``agentbay_*`` identifiers, so callers
must canonicalize tool IDs before comparing grants, allowlists, or runtime
availability.
"""


def canonical_tool_id(tool_id: str) -> str:
    """Return the canonical registry tool identifier for a persisted tool ID."""

    return AGENTBAY_TOOL_ID_ALIASES.get(tool_id, tool_id)


def _merged_unique(*groups: list[str]) -> list[str]:
    """Return a stable union of list entries without duplicates."""

    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return merged


class RoleCapability(BaseModel):
    """Registered policy and capability metadata for a role template.

    The registry separates authorization policy from runtime availability.
    ``allowed_tools`` records what a role may request if the runtime exposes
    those tools; callers must still provide a concrete availability set before
    execution to prove those tools are live in the current environment.
    """

    role: str
    capabilities: list[str] = Field(default_factory=list)
    can_write_product_files: bool = False
    can_vote: bool = False
    can_accept_artifacts: bool = False
    network_policy: NetworkPolicy = "disabled"
    allowed_tools: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    required_validation_checks: list[str] = Field(default_factory=list)
    description: str = ""


class SpecialistSkillReference(BaseModel):
    """Immutable reference to one repository-owned specialist skill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SpecialistResourcePolicy(BaseModel):
    """Bounded execution policy attached to a specialist template."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_assignments: int = Field(default=1, ge=1, le=4)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    network_policy: NetworkPolicy = "disabled"


class SpecialistArtifactContract(BaseModel):
    """Artifact ownership and validation obligations for a specialist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    can_produce: bool
    can_validate: bool
    requires_export_hashes: bool = True
    requires_independent_validation: bool = False


class SpecialistTemplate(BaseModel):
    """Versioned, repository-owned specialist definition.

    Tool and skill bundles are tuples so a resolved template cannot be mutated
    after the leader selects it. The leader sees capabilities and availability,
    but never supplies these policy fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    version: str
    role: str
    description: str
    capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...]
    required_tool_ids: tuple[str, ...]
    skills: tuple[SpecialistSkillReference, ...]
    resource_policy: SpecialistResourcePolicy
    conflict_domains: tuple[str, ...]
    artifact_contract: SpecialistArtifactContract


class ResolvedSpecialistSkill(BaseModel):
    """Verified skill content loaded for exactly one specialist invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str
    version: str
    sha256: str
    content: str


class ResolvedSpecialistBundle(BaseModel):
    """Replay-safe template bundle after repository skill verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: SpecialistTemplate
    skills: tuple[ResolvedSpecialistSkill, ...]
    tool_bundle_hash: str


_SPECIALIST_SKILL_ROOT = Path(__file__).resolve().parent / "skills"


FIXED_SPECIALIST_TEMPLATES = MappingProxyType({
    "builder": SpecialistTemplate(
        template_id="builder",
        version="2",
        role="Implementation Engineer",
        description="Implements bounded repository changes in an isolated execution environment.",
        capabilities=("implementation", "repository_write", "sandbox_execution", "artifact_export"),
        tool_ids=(
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "write_text_file",
            "list_files",
            "export_artifact",
            "close_execution_environment",
        ),
        required_tool_ids=(
            "start_execution_environment",
            "execute_command",
            "export_artifact",
            "close_execution_environment",
        ),
        skills=(
            SpecialistSkillReference(
                skill_id="repository_implementation",
                version="2",
                sha256="57b369c677b252a17144b9c74d34f95773db6505bbb81a7171b9c32399a1c090",
            ),
        ),
        resource_policy=SpecialistResourcePolicy(),
        conflict_domains=("repository_write",),
        artifact_contract=SpecialistArtifactContract(
            can_produce=True,
            can_validate=False,
            requires_independent_validation=True,
        ),
    ),
    "test_engineer": SpecialistTemplate(
        template_id="test_engineer",
        version="1",
        role="Test Engineer",
        description="Independently validates exported artifacts without changing product files.",
        capabilities=("test_execution", "failure_diagnosis", "artifact_inspection", "independent_validation"),
        tool_ids=(
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "list_files",
            "close_execution_environment",
            "inspect_artifact",
            "report_independent_validation",
        ),
        required_tool_ids=(
            "start_execution_environment",
            "execute_command",
            "inspect_artifact",
            "report_independent_validation",
            "close_execution_environment",
        ),
        skills=(
            SpecialistSkillReference(
                skill_id="independent_validation",
                version="1",
                sha256="6a800068976adf87b68f360354f433e105b80db65fe111f3d76b6fd6ada0186c",
            ),
        ),
        resource_policy=SpecialistResourcePolicy(),
        conflict_domains=("artifact_validation",),
        artifact_contract=SpecialistArtifactContract(
            can_produce=False,
            can_validate=True,
            requires_independent_validation=False,
        ),
    ),
    "image_creator": SpecialistTemplate(
        template_id="image_creator",
        version="1",
        role="Image Creator",
        description="Generates, inspects, and publishes durable image artifacts for review.",
        capabilities=("image_generation", "image_inspection", "artifact_publishing"),
        tool_ids=("generate_images", "inspect_image", "publish_image"),
        required_tool_ids=("generate_images", "inspect_image", "publish_image"),
        skills=(
            SpecialistSkillReference(
                skill_id="image_generation",
                version="1",
                sha256="01eca66ac4556a9176b78982f7e1c20127a1f0dc22f7e557097b20b626a4c251",
            ),
        ),
        resource_policy=SpecialistResourcePolicy(),
        conflict_domains=("image_artifact_generation",),
        artifact_contract=SpecialistArtifactContract(
            can_produce=True,
            can_validate=False,
            requires_independent_validation=True,
        ),
    ),
    "frontend_engineer": SpecialistTemplate(
        template_id="frontend_engineer",
        version="2",
        role="Frontend Engineer",
        description="Builds bounded product UI changes and captures browser-render evidence.",
        capabilities=("implementation", "ui_engineering", "browser_testing", "sandbox_execution", "artifact_export"),
        tool_ids=(
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "write_text_file",
            "list_files",
            "browser_render",
            "export_artifact",
            "close_execution_environment",
        ),
        required_tool_ids=(
            "start_execution_environment",
            "execute_command",
            "browser_render",
            "export_artifact",
            "close_execution_environment",
        ),
        skills=(
            SpecialistSkillReference(
                skill_id="frontend_browser_delivery",
                version="1",
                sha256="f893372cee8bce3ab13195720f73cf76b52e11a04221b578b48070233e28dafb",
            ),
        ),
        resource_policy=SpecialistResourcePolicy(),
        conflict_domains=("repository_write", "frontend_build"),
        artifact_contract=SpecialistArtifactContract(
            can_produce=True,
            can_validate=False,
            requires_independent_validation=True,
        ),
    ),
})


def list_fixed_specialist_templates() -> dict[str, SpecialistTemplate]:
    """Return deep copies of the immutable fixed specialist catalog."""

    return {key: value.model_copy(deep=True) for key, value in FIXED_SPECIALIST_TEMPLATES.items()}


def get_fixed_specialist_template(template_id: str) -> SpecialistTemplate | None:
    """Return one fixed template without exposing registry-owned state."""

    template = FIXED_SPECIALIST_TEMPLATES.get(template_id)
    return template.model_copy(deep=True) if template is not None else None


def resolve_specialist_bundle(template_id: str) -> ResolvedSpecialistBundle:
    """Resolve and hash-check a template's exact repository-owned skill bundle.

    Resolution fails before any provider or sandbox call if a referenced skill
    is missing or has drifted from the catalog's pinned content hash.
    """

    template = FIXED_SPECIALIST_TEMPLATES.get(template_id)
    if template is None:
        raise ValueError(f"unknown_specialist_template:{template_id}")

    resolved_skills: list[ResolvedSpecialistSkill] = []
    for reference in template.skills:
        path = (_SPECIALIST_SKILL_ROOT / reference.skill_id / reference.version / "SKILL.md").resolve()
        if _SPECIALIST_SKILL_ROOT not in path.parents or not path.is_file():
            raise ValueError(f"specialist_skill_missing:{reference.skill_id}@{reference.version}")
        content_bytes = path.read_bytes()
        # Git may materialize repository text with CRLF on Windows even though
        # the pinned catalog hash was produced from the canonical LF content.
        # Normalize only line endings before verification so the repository
        # skill remains immutable across supported checkout platforms.
        canonical_content_bytes = content_bytes.replace(b"\r\n", b"\n")
        actual_hash = hashlib.sha256(canonical_content_bytes).hexdigest()
        if actual_hash != reference.sha256:
            raise ValueError(
                f"specialist_skill_hash_mismatch:{reference.skill_id}@{reference.version}:"
                f"expected={reference.sha256}:actual={actual_hash}"
            )
        resolved_skills.append(
            ResolvedSpecialistSkill(
                skill_id=reference.skill_id,
                version=reference.version,
                sha256=actual_hash,
                content=canonical_content_bytes.decode("utf-8"),
            )
        )

    canonical_bundle = {
        "template_id": template.template_id,
        "template_version": template.version,
        "tool_ids": list(template.tool_ids),
        "skills": [
            {"skill_id": skill.skill_id, "version": skill.version, "sha256": skill.sha256}
            for skill in resolved_skills
        ],
    }
    tool_bundle_hash = hashlib.sha256(
        json.dumps(canonical_bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ResolvedSpecialistBundle(
        template=template,
        skills=tuple(resolved_skills),
        tool_bundle_hash=tool_bundle_hash,
    )


CORE_ROLE_CAPABILITIES: dict[str, RoleCapability] = {
    "coordinator": RoleCapability(
        role="coordinator",
        capabilities=[
            "capability_search",
            "team_planning",
            "work_graph_planning",
            "typed_blocker_reporting",
        ],
        can_write_product_files=False,
        can_vote=False,
        can_accept_artifacts=False,
        network_policy="disabled",
        allowed_tools=["capability_search", "team_plan", "work_graph_plan", "typed_blocker"],
        description="Selects the smallest capable team and defines the work graph.",
    ),
    "architect": RoleCapability(
        role="architect",
        capabilities=_merged_unique(
            ["decomposition", "architecture", "tradeoffs", "system_design"],
            ["interface_definition", "file_ownership_planning"],
        ),
        can_write_product_files=False,
        can_vote=True,
        can_accept_artifacts=True,
        network_policy="disabled",
        allowed_tools=_merged_unique(
            ["decompose_task", "risk_assessment"],
            ["repository_read", "dependency_graph", "architecture_artifact"],
        ),
        description="Defines boundaries, interfaces, and file ownership.",
    ),
    "researcher": RoleCapability(
        role="researcher",
        capabilities=_merged_unique(
            ["evidence", "assumption_testing", "context_gathering", "context7_research"],
            ["evidence_gathering", "domain_research", "citation_retrieval"],
        ),
        can_write_product_files=False,
        can_vote=True,
        can_accept_artifacts=True,
        network_policy="allowlist",
        allowed_tools=_merged_unique(
            ["context7_lookup"],
            ["approved_web_lookup", "citation_lookup"],
        ),
        description="Gathers current technical or domain evidence.",
    ),
    "builder": RoleCapability(
        role="builder",
        capabilities=_merged_unique(
            ["prototyping", "integration", "delivery", "code_execution"],
            ["implementation", "repository_write", "sandbox_execution", "artifact_export"],
        ),
        can_write_product_files=True,
        can_vote=True,
        can_accept_artifacts=False,
        network_policy="disabled",
        allowed_tools=_merged_unique(
            ["execute_notes_demo"],
            [
                "repository_read",
                "repository_write",
                "start_execution_environment",
                "execute_command",
                "run_code",
                "read_text_file",
                "write_text_file",
                "list_files",
                "export_artifact",
                "close_execution_environment",
            ],
        ),
        required_tools=[
            "start_execution_environment",
            "execute_command",
            "export_artifact",
            "close_execution_environment",
        ],
        description="Implements or repairs software in an isolated execution environment.",
    ),
    "critic": RoleCapability(
        role="critic",
        capabilities=_merged_unique(
            ["risk_analysis", "quality_gates", "counterarguments", "validation"],
            ["adversarial_review", "artifact_acceptance", "rubric_evaluation"],
        ),
        can_write_product_files=False,
        can_vote=True,
        can_accept_artifacts=True,
        network_policy="disabled",
        allowed_tools=_merged_unique(
            ["risk_assessment", "read_artifacts"],
            ["review_reports", "rubric_check"],
        ),
        description="Performs adversarial review and independent acceptance validation.",
    ),
}

_SPECIALIST_TEMPLATES: dict[str, RoleCapability] = {
    "frontend_engineer": RoleCapability(
        role="frontend_engineer",
        capabilities=[
            "implementation",
            "ui_engineering",
            "browser_testing",
            "sandbox_execution",
            "artifact_export",
        ],
        can_write_product_files=True,
        can_vote=False,
        can_accept_artifacts=False,
        network_policy="disabled",
        allowed_tools=[
            "repository_read",
            "repository_write",
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "write_text_file",
            # Matches the immutable frontend template: browser-render work
            # must enumerate the bounded workspace before selecting an entry.
            "list_files",
            "browser_render",
            "browser_test",
            "export_artifact",
            "close_execution_environment",
        ],
        required_tools=[
            "start_execution_environment",
            "execute_command",
            "export_artifact",
            "close_execution_environment",
        ],
        description="Builds UI behavior and runs browser-focused checks.",
    ),
    "test_engineer": RoleCapability(
        role="test_engineer",
        capabilities=[
            "test_execution",
            "failure_diagnosis",
            "sandbox_execution",
            "artifact_inspection",
            "independent_validation",
        ],
        can_write_product_files=False,
        can_vote=False,
        can_accept_artifacts=True,
        network_policy="disabled",
        allowed_tools=[
            "repository_read",
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "list_files",
            "browser_render",
            "close_execution_environment",
            "inspect_artifact",
            "report_independent_validation",
        ],
        required_tools=[
            "start_execution_environment",
            "execute_command",
            "inspect_artifact",
            "close_execution_environment",
        ],
        description="Executes tests and reports failures without changing product files.",
    ),
    "image_creator": RoleCapability(
        role="image_creator",
        capabilities=["image_generation", "image_inspection", "artifact_publishing"],
        can_write_product_files=False,
        can_vote=False,
        can_accept_artifacts=False,
        network_policy="disabled",
        allowed_tools=["generate_images", "generate_image", "inspect_image", "publish_image"],
        required_tools=["generate_images", "inspect_image", "publish_image"],
        required_validation_checks=["artifact_collection", "artifact_provenance"],
        description="Generates or edits image artifacts and publishes them for review.",
    ),
    "video_producer": RoleCapability(
        role="video_producer",
        capabilities=["video_generation", "media_inspection", "artifact_publishing"],
        can_write_product_files=False,
        can_vote=False,
        can_accept_artifacts=False,
        network_policy="disabled",
        allowed_tools=[
            "submit_text_to_video",
            "get_video_job",
            "cancel_video_job",
            "collect_video",
            "inspect_video",
        ],
        required_tools=["submit_text_to_video", "get_video_job", "collect_video", "inspect_video"],
        required_validation_checks=["artifact_collection", "artifact_provenance"],
        description="Generates videos and records the collection path for provenance.",
    ),
    "data_analyst": RoleCapability(
        role="data_analyst",
        capabilities=["data_analysis", "python_analysis", "sandbox_execution", "artifact_export"],
        can_write_product_files=False,
        can_vote=False,
        can_accept_artifacts=False,
        network_policy="disabled",
        allowed_tools=[
            "repository_read",
            "start_execution_environment",
            "run_code",
            "read_text_file",
            "list_files",
            "data_file_read",
            "table_artifact",
            "chart_artifact",
            "export_artifact",
            "close_execution_environment",
        ],
        required_tools=[
            "start_execution_environment",
            "run_code",
            "export_artifact",
            "close_execution_environment",
        ],
        description="Analyzes datasets and exports evidence artifacts.",
    ),
}

_ROLE_ALIASES = {
    "test_validation_engineer": "test_engineer",
}


def get_role_capabilities(role_key: str) -> RoleCapability | None:
    """Look up capability metadata by role key, including compatibility aliases."""

    canonical_key = _ROLE_ALIASES.get(role_key, role_key)
    if canonical_key in CORE_ROLE_CAPABILITIES:
        return CORE_ROLE_CAPABILITIES[canonical_key]
    return _SPECIALIST_TEMPLATES.get(canonical_key)


def register_specialist_template(template: RoleCapability) -> None:
    """Register a specialist template, normalizing compatibility aliases.

    ``test_validation_engineer`` is a legacy alias for ``test_engineer``.
    Registering through the alias updates the canonical key so callers do not
    accidentally create two diverging registry entries for the same template.
    """

    canonical_role = _ROLE_ALIASES.get(template.role, template.role)
    _SPECIALIST_TEMPLATES[canonical_role] = template.model_copy(update={"role": canonical_role})


def list_specialist_templates() -> dict[str, RoleCapability]:
    """Return a copy of all registered specialist templates."""

    return dict(_SPECIALIST_TEMPLATES)


def capabilities_satisfy_requirements(
    capabilities: list[str],
    required: list[str],
) -> bool:
    """Return True when *capabilities* cover every entry in *required*."""

    cap_set = set(capabilities)
    return all(req in cap_set for req in required)


def find_capable_team_member(
    team_member_ids: list[str],
    agents: dict[str, Any],
    required_capabilities: list[str],
    exclude_ids: set[str] | None = None,
) -> str | None:
    """Find a nonduplicate team member whose registry entry satisfies requirements."""

    exclude = exclude_ids or set()
    for member_id in team_member_ids:
        if member_id in exclude:
            continue
        agent = agents.get(member_id)
        if agent is None:
            continue
        role_key = resolve_role_key(agent)
        reg = get_role_capabilities(role_key)
        if reg is None:
            continue
        if capabilities_satisfy_requirements(reg.capabilities, required_capabilities):
            return member_id
    return None


def resolve_role_key(agent: Any) -> str:
    """Map an agent to its registry role key."""

    agent_id = getattr(agent, "id", "")
    if get_role_capabilities(agent_id) is not None:
        return _ROLE_ALIASES.get(agent_id, agent_id)

    role_text = (
        f"{agent_id} {getattr(agent, 'role', '')} {' '.join(getattr(agent, 'skills', []))}"
    ).lower()
    parent_id = getattr(agent, "parent_id", None)
    normalized_role_text = role_text.replace("/", " ").replace("-", " ")
    if "test validation engineer" in normalized_role_text:
        return "test_validation_engineer"
    if parent_id:
        for template_key in _SPECIALIST_TEMPLATES:
            words = template_key.replace("_", " ").split()
            if all(word in normalized_role_text for word in words):
                return template_key

    if "coordinator" in normalized_role_text:
        return "coordinator"
    if "research" in normalized_role_text or "evidence" in normalized_role_text:
        return "researcher"
    if "critic" in normalized_role_text or "review" in normalized_role_text or "validation" in normalized_role_text:
        return "critic"
    if "architect" in normalized_role_text or "interface" in normalized_role_text:
        return "architect"
    if "frontend" in normalized_role_text or "browser" in normalized_role_text or "ui" in normalized_role_text:
        return "frontend_engineer"
    if "test engineer" in normalized_role_text or "qa" in normalized_role_text:
        return "test_engineer"
    if "image" in normalized_role_text or "visual" in normalized_role_text:
        return "image_creator"
    if "video" in normalized_role_text:
        return "video_producer"
    if "data" in normalized_role_text or "analysis" in normalized_role_text:
        return "data_analyst"
    if "implement" in normalized_role_text or "build" in normalized_role_text or "engineer" in normalized_role_text:
        return "builder"
    return agent_id


def default_required_capabilities(role_key: str) -> list[str]:
    """Return the minimal capability needed to take over a role's subtask."""

    canonical_key = _ROLE_ALIASES.get(role_key, role_key)
    return {
        "coordinator": ["team_planning"],
        "architect": ["architecture"],
        "researcher": ["evidence_gathering"],
        "builder": ["implementation"],
        "frontend_engineer": ["ui_engineering"],
        "test_engineer": ["test_execution"],
        "image_creator": ["image_generation"],
        "video_producer": ["video_generation"],
        "data_analyst": ["data_analysis"],
        "critic": ["validation"],
    }.get(canonical_key, [])
