from __future__ import annotations

import re
import shlex
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import Settings


def _text_from_tool_result(result: Any) -> str:
    """Extract readable text from an MCP tool result."""

    content = getattr(result, "content", None)
    if not content:
        return str(result)
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts) or str(result)


def _first_library_id(text: str) -> str | None:
    """Return the first Context7 library id from resolver output."""

    match = re.search(r"Context7-compatible library ID:\s*(/[^\s]+)", text)
    return match.group(1) if match else None


async def collect_context7_evidence(settings: Settings, query: str) -> dict[str, Any]:
    """Collect Context7 evidence deterministically for researcher turns.

    This deliberately uses the MCP client directly instead of asking the model
    to decide whether to call Context7. The model receives the retrieved
    evidence afterwards and must reason from it.
    """

    command_parts = shlex.split(settings.context7_mcp_command)
    if not command_parts:
        return {
            "success": False,
            "error": "CONTEXT7_MCP_COMMAND is empty.",
            "tool_calls": [],
        }

    tool_calls: list[dict[str, Any]] = []
    params = StdioServerParameters(command=command_parts[0], args=command_parts[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            focused_query = (
                "Agno Python MCPTools setup for Agent with MCP server. "
                "Need imports from agno.tools.mcp, stdio command examples, "
                "streamable HTTP/SSE examples, connect/initialize lifecycle, "
                "and tools=[mcp_tools] Agent binding. "
                f"User task context: {query[:1000]}"
            )

            resolve_args = {
                "libraryName": "Agno",
                "query": focused_query,
            }
            resolved = await session.call_tool("resolve-library-id", resolve_args)
            resolved_text = _text_from_tool_result(resolved)
            tool_calls.append({
                "tool_name": "resolve-library-id",
                "arguments": resolve_args,
                "result": resolved_text[:4000],
            })

            library_id = _first_library_id(resolved_text) or "/agno-agi/docs"
            docs_args = {
                "libraryId": library_id,
                "query": focused_query,
            }
            docs = await session.call_tool("query-docs", docs_args)
            docs_text = _text_from_tool_result(docs)
            tool_calls.append({
                "tool_name": "query-docs",
                "arguments": docs_args,
                "result": docs_text[:8000],
            })

    return {
        "success": True,
        "library_id": tool_calls[-1]["arguments"]["libraryId"],
        "tool_calls": tool_calls,
    }


def format_context7_evidence(evidence: dict[str, Any]) -> str:
    """Format collected evidence for inclusion in a researcher prompt."""

    if not evidence.get("success"):
        return f"Context7 evidence collection failed: {evidence.get('error', 'unknown error')}"

    lines = [
        "Context7 evidence collected by the backend before this researcher turn.",
        f"Resolved library id: {evidence.get('library_id', 'unknown')}",
    ]
    for call in evidence.get("tool_calls", []):
        lines.append(f"\nTool: {call.get('tool_name')}")
        lines.append(f"Arguments: {call.get('arguments')}")
        lines.append(f"Result excerpt:\n{call.get('result', '')}")
    return "\n".join(lines)
