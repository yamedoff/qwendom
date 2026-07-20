from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Callable

from agno.agent import Agent
from agno.models.cerebras import Cerebras
from agno.models.dashscope import DashScope
from agno.models.openrouter import OpenRouter

from config import Settings
from .db import get_agno_db
from .knowledge import load_role_knowledge
from .tools.capabilities import execute_notes_demo_tool
from .context7_research import build_context7_lookup_tool
from .models import SocietyAgent


RESEARCHER_CONTEXT7_INSTRUCTIONS = [
    "Context7 is a permitted evidence source, not a mandatory path. Decide whether local context is sufficient before using it.",
    "Local context means the prompt, session state, and any supplied knowledge. Do not invent or call unprovided local-search tools.",
    "If external library documentation is necessary, call context7_lookup with your objective, precise query, and expected evidence.",
    "The only external evidence tool available in this turn is context7_lookup.",
    "Separate retrieved facts from inference, preserve source provenance, and never invent citations.",
    "If evidence is irrelevant or insufficient, refine once or report an honest evidence gap.",
]


def build_model(settings: Settings) -> Cerebras | DashScope | OpenRouter:
    """Create the native Agno model provider for the active backend."""

    if settings.provider == "cerebras":
        return Cerebras(
            id=settings.cerebras_model,
            api_key=settings.cerebras_api_key,
        )
    if settings.provider == "openrouter":
        return OpenRouter(
            id=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
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
    output_schema: type[BaseModel] | None = None,
    structured_outputs: bool | None = None,
    extra_instructions: list[str] | None = None,
    tool_call_limit: int | None = None,
    session_id: str | None = None,
    session_state: dict[str, Any] | None = None,
) -> Agent:
    """Create an Agno agent from a society identity.

    Cerebras uses Agno's native Cerebras provider. Qwen Cloud uses Agno's
    native DashScope provider. OpenRouter uses Agno's native OpenRouter
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
    if output_schema is not None:
        kwargs["output_schema"] = output_schema
    if structured_outputs is not None:
        kwargs["structured_outputs"] = structured_outputs
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

    if settings.benchmark_suite_tools_enabled:
        if settings.benchmark_suite_version == "v3":
            return [
                "For benchmark v3, inspect the public security surfaces with the provided read-only tools. "
                "Cite only record IDs returned by tools and do not invent evidence."
            ]
        return [
            "For benchmark tasks, you may use lookup_record, lookup_dataset, and calculate. "
            "They expose only public task data and basic arithmetic."
        ]
    if researcher_uses_context7(identity, settings):
        return RESEARCHER_CONTEXT7_INSTRUCTIONS.copy()
    if settings.role_specific_tools_enabled and identity.id == "builder":
        return [
            "When the task requires an executable collaborative-notes implementation, call execute_notes_demo. "
            "Never claim files or validation commands ran unless that tool returns executed=true and passed=true."
        ]
    return []


@asynccontextmanager
async def role_tool_context(
    identity: SocietyAgent,
    settings: Settings,
    *,
    on_tool_intent: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[list[Any]]:
    """Open optional role-specific toolkits for a single Agno run.

    Agno's MCPTools owns a live MCP client connection, so callers should create
    it around the agent run instead of storing it on long-lived society agents.
    """

    tools: list[Any] = []
    if settings.benchmark_suite_tools_enabled:
        if settings.benchmark_suite_version == "v3":
            from benchmarks.tools_v3 import SHARED_TOOLS
            tools.extend(SHARED_TOOLS)
        else:
            from benchmarks.tools_v2 import calculate, lookup_dataset, lookup_record
            tools.extend([lookup_record, lookup_dataset, calculate])
    if settings.role_specific_tools_enabled and identity.id == "builder":
        tools.append(execute_notes_demo_tool)
    if researcher_uses_context7(identity, settings):
        tools.append(build_context7_lookup_tool(
            settings,
            max_calls=settings.context7_max_calls_per_turn,
            on_intent=on_tool_intent,
            on_result=on_tool_result,
        ))
    yield tools


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
