from __future__ import annotations

import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agno.agent import Agent
from agno.models.qwen_legacy import Qwen Legacy
from agno.models.dashscope import DashScope
from agno.models.qwen_legacy import Qwen Legacy

from config import Settings
from .db import get_agno_db
from .knowledge import load_role_knowledge
from .models import SocietyAgent


RESEARCHER_CONTEXT7_INSTRUCTIONS = [
    "When the task depends on current library, framework, SDK, CLI, or cloud-service behavior, use the Context7 MCP tools before making technical claims.",
    "Separate Context7-backed facts from inference, and cite the library or documentation topic you looked up.",
    "Do not use Context7 for ordinary reasoning when local task context is sufficient.",
]


def build_model(settings: Settings) -> Qwen Legacy | DashScope | Qwen Legacy:
    """Create the native Agno model provider for the active backend."""

    if settings.provider == "qwen_legacy":
        return Qwen Legacy(
            id=settings.qwen_legacy_model,
            api_key=settings.qwen_legacy_api_key,
        )
    if settings.provider == "qwen_legacy":
        return Qwen Legacy(
            id=settings.qwen_legacy_model,
            api_key=settings.qwen_legacy_api_key,
            base_url=settings.qwen_legacy_base_url,
        )
    return DashScope(
        id=settings.qwen_model,
        api_key=settings.qwen_api_key,
        base_url=settings.qwen_base_url,
    )


def build_agno_agent(
    identity: SocietyAgent,
    settings: Settings,
    tools: list[Any] | None = None,
    tool_choice: Any | None = None,
    extra_instructions: list[str] | None = None,
    tool_call_limit: int | None = None,
    session_id: str | None = None,
    session_state: dict[str, Any] | None = None,
) -> Agent:
    """Create an Agno agent from a society identity.

    Qwen Legacy uses Agno's native Qwen Legacy provider. Qwen Cloud uses Agno's
    native DashScope provider. Qwen Legacy uses Agno's native Qwen Legacy
    provider. Set ``LLM_PROVIDER`` in ``.env`` to switch.
    """

    instructions = [
        f"You are {identity.name}, a specialist in {', '.join(identity.skills)}.",
        "Argue honestly. If another agent's proposal is weak, identify the flaw.",
        "Keep responses concise and useful for a multi-agent society.",
    ]
    if settings.agent_profiles_enabled:
        instructions.extend(_profile_instructions(identity))
    instructions.extend(extra_instructions or [])

    kwargs: dict[str, Any] = dict(
        name=identity.name,
        role=identity.role,
        model=build_model(settings),
        instructions=instructions,
        markdown=True,
        db=get_agno_db(settings),
        enable_agentic_memory=True,
        update_memory_on_run=True,
        add_memories_to_context=True,
    )

    if session_id is not None:
        kwargs["session_id"] = session_id
    if session_state is not None:
        kwargs["session_state"] = session_state
        kwargs["add_session_state_to_context"] = True

    knowledge = load_role_knowledge(identity.role, settings.knowledge_dir)
    if knowledge is not None:
        kwargs["knowledge"] = knowledge
        kwargs["add_knowledge_to_context"] = True

    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if tool_call_limit is not None:
        kwargs["tool_call_limit"] = tool_call_limit

    return Agent(**kwargs)


def researcher_uses_context7(identity: SocietyAgent, settings: Settings) -> bool:
    """Return whether this identity should receive Context7 MCP tools."""

    return (
        settings.role_specific_tools_enabled
        and settings.context7_mcp_enabled
        and identity.id == "researcher"
    )


def role_tool_instructions(identity: SocietyAgent, settings: Settings) -> list[str]:
    """Provide role-specific instructions that match optional role tools."""

    if researcher_uses_context7(identity, settings):
        return RESEARCHER_CONTEXT7_INSTRUCTIONS.copy()
    return []


@asynccontextmanager
async def role_tool_context(identity: SocietyAgent, settings: Settings) -> AsyncIterator[list[Any]]:
    """Open optional role-specific toolkits for a single Agno run.

    Agno's MCPTools owns a live MCP client connection, so callers should create
    it around the agent run instead of storing it on long-lived society agents.
    """

    if not researcher_uses_context7(identity, settings):
        yield []
        return

    try:
        from agno.tools.mcp import MCPTools
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "Context7 MCP access requires Agno's MCP extras. Install backend dependencies "
            "from backend/requirements.txt and ensure the 'mcp' package is available."
        ) from exc

    command_parts = shlex.split(settings.context7_mcp_command)
    if not command_parts:
        raise RuntimeError("CONTEXT7_MCP_COMMAND cannot be empty when Context7 MCP is enabled.")

    server_params = StdioServerParameters(
        command=command_parts[0],
        args=command_parts[1:],
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            context7_tools = MCPTools(session=session)
            await context7_tools.initialize()
            yield [context7_tools]


def fallback_contribution(identity: SocietyAgent, prompt: str) -> str:
    """Deterministic no-secret contribution for local demos and CI."""

    skill = identity.skills[0]
    blocker = identity.profile.default_blockers[0] if identity.profile.default_blockers else "unclear success criteria"
    bias = identity.profile.decision_bias or "make a useful, defensible contribution"
    style = identity.profile.communication_style or "concise and collaborative"
    return (
        f"{identity.name} applies {skill} with a {style} style. "
        f"Bias: {bias}. "
        f"Contribution: clarify the problem, propose a concrete solution path, "
        f"and flag '{blocker}' as the blocker to watch in '{prompt[:120]}'."
    )


def _profile_instructions(identity: SocietyAgent) -> list[str]:
    """Convert a society profile into stable collaboration instructions."""

    profile = identity.profile
    values = ", ".join(profile.values) or "useful collaboration"
    blockers = "; ".join(profile.default_blockers) or "critical ambiguity or unsafe execution"
    deferrals = "; ".join(
        f"{agent_id} for {', '.join(domains)}"
        for agent_id, domains in profile.defers_to.items()
    ) or "the teammate with stronger evidence for a domain"
    return [
        f"Your work values are: {values}.",
        f"Your communication style is {profile.communication_style or 'concise and collaborative'}.",
        f"Your risk tolerance is {profile.risk_tolerance}.",
        f"Your decision bias: {profile.decision_bias or 'make a useful, defensible contribution'}.",
        f"Default blockers you should raise when relevant: {blockers}.",
        f"Defer explicitly to: {deferrals}.",
        f"Watch your own failure mode: {profile.failure_mode or 'overstating confidence'}.",
        "Do not agree for politeness. Support, challenge, defer, or block based on your profile and the task evidence.",
        "If another agent changes your mind, say what changed and why.",
    ]
