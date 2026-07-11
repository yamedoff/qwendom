from __future__ import annotations

import asyncio
import json
from typing import Any

from agno.agent import Agent
from agno.tools import tool
from pydantic import BaseModel, Field, ValidationError

from config import Settings, get_settings
from .agents import build_model


class PreflightDecision(BaseModel):
    """Validated result for the native tool-call smoke test."""

    choice: str = Field(pattern="^(alpha|beta)$")
    reason: str = Field(min_length=1)


@tool(name="record_decision", stop_after_tool_call=True)
def record_decision(choice: str, reason: str) -> str:
    """Record a required preflight decision through a native Agno tool."""

    return PreflightDecision(choice=choice, reason=reason).model_dump_json()


def _extract_record_decision(response: Any) -> PreflightDecision:
    """Return the explicit native tool result or raise if the model used prose."""

    tools_list = getattr(response, "tools", None)
    if not isinstance(tools_list, list):
        raise RuntimeError("Preflight failed: response did not expose native tool calls")

    for tool_entry in tools_list:
        entry_name = getattr(tool_entry, "tool_name", None) or getattr(tool_entry, "name", None)
        if entry_name and "record_decision" in str(entry_name):
            raw_result = getattr(tool_entry, "result", None)
            if raw_result is None:
                raise RuntimeError("Preflight failed: native tool call had no result")
            try:
                payload = json.loads(str(raw_result))
                return PreflightDecision.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise RuntimeError(f"Preflight failed: invalid tool result: {exc}") from exc

    raise RuntimeError("Preflight failed: model did not call record_decision")


async def run_preflight(settings: Settings | None = None) -> PreflightDecision:
    """Verify the configured model can execute a forced native Agno tool call."""

    runtime_settings = settings or get_settings()
    if not runtime_settings.llm_enabled:
        raise RuntimeError("Preflight requires an API key; deterministic no-key mode is not a native tool-call path")

    agent = Agent(
        name="Governance Preflight",
        role="Native tool-call verifier",
        model=build_model(runtime_settings),
        tools=[record_decision],
        tool_choice={"type": "function", "function": {"name": "record_decision"}},
        tool_call_limit=1,
        instructions=[
            "You must call the record_decision tool exactly once.",
            "Do not answer in prose.",
            "Choose either alpha or beta and put the choice and reason into the tool arguments.",
        ],
    )

    response = await asyncio.wait_for(
        agent.arun("Choose alpha or beta for this native tool-call smoke test."),
        timeout=runtime_settings.llm_timeout_seconds,
    )
    return _extract_record_decision(response)


def main() -> None:
    decision = asyncio.run(run_preflight())
    print(f"native_tool_preflight=passed choice={decision.choice}")


if __name__ == "__main__":
    main()
