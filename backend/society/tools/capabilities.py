from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

from agno.run import RunContext
from agno.tools import tool

from ..schemas.capabilities import (
    ImplementationPlan,
    MemoryLookup,
    MemoryWrite,
    RiskAssessment,
    TaskDecomposition,
)


def _as_list(value: list[Any] | str | None) -> list[str]:
    """Normalize model-provided list arguments that arrive as bullet strings."""

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip(" -\t") for line in value.splitlines() if line.strip(" -\t")]


def load_notes_demo_evidence(task_id: str) -> dict[str, Any] | None:
    """Load durable execution evidence emitted by ``execute_notes_demo``."""

    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
    path = Path(__file__).resolve().parents[2] / "data" / "runtime_artifacts" / safe_id / "evidence.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@tool(name="decompose_task", stop_after_tool_call=True)
def decompose_task_tool(
    objective: str,
    steps: list[str] | str,
    delegation_plan: dict[str, str] | None = None,
    run_context: RunContext | None = None,
) -> str:
    """Record a task decomposition for the current team.

    Args:
        objective: The clarified objective the team should solve.
        steps: Ordered work steps.
        delegation_plan: Mapping from agent id or role to responsibility.

    Returns:
        JSON string with the validated decomposition.
    """

    result = TaskDecomposition(
        objective=objective,
        steps=_as_list(steps),
        delegation_plan=delegation_plan or {},
    )
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("subtasks", []).extend(
            {
                "id": f"subtask-{uuid4().hex[:10]}",
                "agent_id": agent_id,
                "subtask": subtask,
                "status": "planned",
            }
            for agent_id, subtask in result.delegation_plan.items()
        )
    return result.model_dump_json()


@tool(name="memory_lookup", stop_after_tool_call=True)
def memory_lookup_tool(
    query: str,
    relevant_memories: list[Any] | str | None,
    lesson: str,
    run_context: RunContext | None = None,
) -> str:
    """Record context retrieved from an agent's collaboration memory.

    Args:
        query: The memory/context question being answered.
        relevant_memories: Relevant remembered facts or prior lessons.
        lesson: The main lesson to apply to the current task.

    Returns:
        JSON string with the validated memory lookup.
    """

    result = MemoryLookup(query=query, relevant_memories=_as_list(relevant_memories), lesson=lesson)
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("memory_reads", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="implementation_plan", stop_after_tool_call=True)
def implementation_plan_tool(
    artifact: str,
    milestones: list[str] | str,
    acceptance_checks: list[str] | str,
    run_context: RunContext | None = None,
) -> str:
    """Record a concrete implementation plan.

    Args:
        artifact: The artifact or deliverable to produce.
        milestones: Ordered implementation milestones.
        acceptance_checks: Checks proving the artifact is useful.

    Returns:
        JSON string with the validated implementation plan.
    """

    result = ImplementationPlan(
        artifact=artifact,
        milestones=_as_list(milestones),
        acceptance_checks=_as_list(acceptance_checks),
    )
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("implementation_plans", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="execute_notes_demo", stop_after_tool_call=True)
def execute_notes_demo_tool(
    architecture: str,
    run_context: RunContext | None = None,
) -> str:
    """Build and execute a bounded collaborative-notes implementation slice.

    The tool intentionally supports only the server-authoritative comparison
    slice. It writes fixed, inspectable source under ``data/runtime_artifacts``,
    launches it on an ephemeral localhost port, verifies health plus a real
    write/read round-trip, and always terminates the child process.

    Args:
        architecture: Must be ``centralized`` for this bounded executable slice.

    Returns:
        JSON execution evidence containing paths, command, output, and checks.
    """

    if architecture.strip().lower() != "centralized":
        raise ValueError("execute_notes_demo currently supports only architecture='centralized'")
    session_id = str(getattr(run_context, "session_id", "standalone") or "standalone")
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)
    artifact_dir = Path(__file__).resolve().parents[2] / "data" / "runtime_artifacts" / safe_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server_path = artifact_dir / "server.py"
    client_path = artifact_dir / "client.html"
    server_path.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import json, sys\n"
        "note = {'text': ''}\n"
        "class H(BaseHTTPRequestHandler):\n"
        "  def log_message(self, *_): pass\n"
        "  def _send(self, status, body):\n"
        "    data=json.dumps(body).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)\n"
        "  def do_GET(self):\n"
        "    self._send(200, {'status':'ok','sync':'server-authoritative'} if self.path=='/health' else note)\n"
        "  def do_POST(self):\n"
        "    global note; note=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}'); self._send(200,note)\n"
        "HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n",
        encoding="utf-8",
    )
    client_path.write_text(
        "<!doctype html><textarea id='note'></textarea><script>"
        "const n=document.querySelector('#note');"
        "fetch('/note').then(r=>r.json()).then(v=>n.value=v.text||'');"
        "n.oninput=()=>fetch('/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:n.value})});"
        "</script>",
        encoding="utf-8",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    command = [sys.executable, str(server_path), str(port)]
    process = subprocess.Popen(command, cwd=artifact_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        health: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    health = json.load(response)
                break
            except Exception:
                time.sleep(0.1)
        if health is None:
            raise RuntimeError("demo server did not become healthy")
        body = json.dumps({"text": "qwendom-proof"}).encode()
        request = Request(
            f"http://127.0.0.1:{port}/note",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            written = json.load(response)
        with urlopen(f"http://127.0.0.1:{port}/note", timeout=2) as response:
            read_back = json.load(response)
        passed = health.get("status") == "ok" and written == read_back == {"text": "qwendom-proof"}
        evidence = {
            "executed": True,
            "passed": passed,
            "architecture": "centralized",
            "artifact_dir": str(artifact_dir),
            "files": [str(server_path), str(client_path)],
            "command": command,
            "health": health,
            "round_trip": read_back,
        }
        (artifact_dir / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        if run_context is not None:
            run_context.session_state.setdefault("execution_evidence", []).append(evidence)
        return json.dumps(evidence)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


@tool(name="risk_assessment", stop_after_tool_call=True)
def risk_assessment_tool(
    risks: list[str] | str,
    mitigations: list[str] | str,
    quality_gate: str,
    run_context: RunContext | None = None,
) -> str:
    """Record risks and quality gates for the proposed solution.

    Args:
        risks: Important failure modes or unsupported assumptions.
        mitigations: Practical mitigations for the listed risks.
        quality_gate: The minimum check that must pass before accepting the work.

    Returns:
        JSON string with the validated risk assessment.
    """

    result = RiskAssessment(risks=_as_list(risks), mitigations=_as_list(mitigations), quality_gate=quality_gate)
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("risk_assessments", []).append(result.model_dump())
    return result.model_dump_json()


@tool(name="memory_write", stop_after_tool_call=True)
def memory_write_tool(memory: str, tags: list[str] | str | None = None, run_context: RunContext | None = None) -> str:
    """Record a durable collaboration lesson.

    Args:
        memory: The lesson or collaboration fact to preserve.
        tags: Optional labels for future retrieval.

    Returns:
        JSON string with the validated memory write.
    """

    result = MemoryWrite(memory=memory, tags=_as_list(tags))
    if run_context is not None:
        state = run_context.session_state
        state.setdefault("memory_writes", []).append(result.model_dump())
    return result.model_dump_json()
