from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import sys
import threading
from pathlib import Path
from typing import Any


def _configure_utf8_stream(stream: object) -> None:
    """Prefer UTF-8 for runtime logs without assuming a real console stream.

    Windows can otherwise select cp1252 for redirected output, which makes
    genuine provider errors containing non-ASCII punctuation raise a secondary
    ``UnicodeEncodeError`` inside Rich logging. Test capture streams and other
    wrappers may not expose ``reconfigure``; those are intentionally left alone.
    """

    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (OSError, ValueError):
        # A detached, closed, or already-configured stream must not prevent the
        # API from starting. Provider errors still flow through unchanged.
        return


_configure_utf8_stream(sys.stdout)
_configure_utf8_stream(sys.stderr)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import get_settings
from society import SocietyOrchestrator
from society.models import ClarificationRequest, TaskRequest, derive_task_status
from society.provider_preflight import model_capability_preflight
from society.provider_runtime import redact_provider_error
from society.projections import project_cockpit, project_dossier, project_recap, project_review

logger = logging.getLogger(__name__)

settings = get_settings()
society = SocietyOrchestrator(settings)

_background_tasks: set[asyncio.Task] = set()
# Admission controls only work executing in this process. Persisted/replayed
# waiting-for-user tasks remain visible but cannot be resumed by this product
# flow, so they must never block a fresh autonomous mission after restart.
_ACTIVE_MISSION_STATUSES = {"queued", "running", "remediation"}
_mission_submission_lock = threading.Lock()
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()

_SCHEDULE_DELAY_SECONDS = 0.05

_INLINE_MEDIA_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "video/mp4", "video/webm", "video/ogg",
}


def _safe_artifact_display_path(value: object, fallback: Path) -> str:
    """Return a public relative artifact path without exposing local storage paths."""

    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("\\", "/")
        candidate = Path(raw)
        if not candidate.is_absolute() and all(part not in {"", ".", ".."} for part in candidate.parts):
            return "/".join(candidate.parts)
    return fallback.name


def _artifact_media_type(display_path: str, kind: object) -> str:
    """Choose a conservative public media type from a durable artifact name."""

    guessed, _ = mimetypes.guess_type(display_path)
    if guessed in _INLINE_MEDIA_TYPES:
        return guessed
    if guessed and guessed.startswith("text/"):
        return "text/plain"
    if isinstance(kind, str) and kind.lower() in {"code", "text", "patch", "json", "report"}:
        return "text/plain"
    suffix = Path(display_path).suffix.lower()
    if suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh", ".ps1", ".sql"}:
        return "text/plain"
    return "application/octet-stream"


def _apply_artifact_validation_statuses(records: list[dict[str, Any]], task_events: list[Any]) -> None:
    """Attach explicit validation outcomes without returning validation paths.

    Validation is attached only when an emitted event names an artifact. A
    task-level validation report without artifact references is not evidence
    that any individual export passed. Absolute inspection references are used
    only for internal resolved-path comparison and are never returned.
    """

    saw_validation = False
    by_id = {record["id"]: record for record in records}
    by_relative = {record["relative_path"]: record for record in records}
    by_filename: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_filename.setdefault(record["filename"], []).append(record)
    for event in task_events:
        if event.type not in {"artifact_validated", "local_independent_validation_reported"}:
            continue
        saw_validation = True
        payload = event.payload if isinstance(event.payload, dict) else {}
        status = "passed" if payload.get("passed", True) else "failed"
        references: list[object] = [payload.get("artifact_id"), payload.get("artifact_ref")]
        references.extend(payload.get("artifact_ids") if isinstance(payload.get("artifact_ids"), list) else [])
        references.extend(payload.get("inspected_artifact_refs") if isinstance(payload.get("inspected_artifact_refs"), list) else [])
        for reference in references:
            if isinstance(reference, dict):
                reference = reference.get("id") or reference.get("workspace_relative_path") or reference.get("path")
            if not isinstance(reference, str) or not reference:
                continue
            normalized = reference.replace("\\", "/")
            matched = by_id.get(normalized) or by_relative.get(normalized)
            if matched is None:
                try:
                    inspected_path = Path(reference).resolve()
                except (OSError, ValueError):
                    inspected_path = None
                if inspected_path is not None:
                    matched = next((record for record in records if record["_path"] == inspected_path), None)
            if matched is None and "/" in normalized and not Path(normalized).is_absolute():
                # Validators report durable task-relative storage references
                # (for example ``agentbay/<run>/export.html``). Match those
                # against the already verified absolute record path.
                normalized_suffix = f"/{normalized.lstrip('/')}"
                matched = next(
                    (record for record in records if record["_path"].as_posix().endswith(normalized_suffix)),
                    None,
                )
            if matched is None:
                candidates = by_filename.get(Path(normalized).name, [])
                if len(candidates) == 1:
                    matched = candidates[0]
            if matched is not None:
                matched["validation_status"] = status
    if saw_validation:
        for record in records:
            if record["validation_status"] == "not_validated":
                record["validation_status"] = "pending"


def _artifact_records(task_id: str, task_events: list[Any]) -> list[dict[str, Any]]:
    """Normalize AgentBay export events and verify task-owned bytes before use.

    This is the sole filesystem resolver for artifact API reads. It accepts only
    relative paths beneath the task's durable root, rejects symlinks, and
    verifies the event's SHA-256 every time a present artifact is returned.
    """

    artifact_root = society._composition_artifact_root(task_id).resolve()
    records: dict[str, dict[str, Any]] = {}
    for event in task_events:
        if event.type != "agentbay_artifact_exported":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        ref = payload.get("artifact_ref")
        if not isinstance(ref, dict):
            continue
        artifact_id = ref.get("id")
        expected_sha256 = ref.get("sha256")
        storage_path = ref.get("storage_path") or ref.get("path")
        if not isinstance(artifact_id, str) or not artifact_id or not isinstance(expected_sha256, str) or not isinstance(storage_path, str):
            continue
        candidate = Path(storage_path)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            continue
        resolved = (artifact_root / candidate).resolve()
        if resolved == artifact_root or artifact_root not in resolved.parents:
            continue
        display_path = _safe_artifact_display_path(payload.get("workspace_relative_path"), resolved)
        media_type = _artifact_media_type(display_path, ref.get("type"))
        record = {
            "id": artifact_id,
            "filename": Path(display_path).name,
            "path": display_path,
            "relative_path": display_path,
            "kind": ref.get("type") if isinstance(ref.get("type"), str) else "file",
            "media_type": media_type,
            "size_bytes": ref.get("size_bytes") if isinstance(ref.get("size_bytes"), int) and not isinstance(ref.get("size_bytes"), bool) else None,
            "sha256": expected_sha256,
            "producer": (
                ref.get("producer") if isinstance(ref.get("producer"), str)
                else payload.get("producer") if isinstance(payload.get("producer"), str)
                else payload.get("role_key") if isinstance(payload.get("role_key"), str)
                else None
            ),
            "validation_status": "not_validated",
            "status": "missing",
            "download_url": f"/tasks/{task_id}/artifacts/{artifact_id}",
            "view_url": None,
            "_path": resolved,
        }
        if resolved.is_file() and not resolved.is_symlink():
            actual_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if actual_sha256 == expected_sha256:
                record["status"] = "available"
                record["size_bytes"] = resolved.stat().st_size
                if media_type == "text/plain" or media_type in _INLINE_MEDIA_TYPES:
                    record["view_url"] = f"/tasks/{task_id}/artifacts/{artifact_id}/view"
            else:
                record["status"] = "integrity_failed"
        # Later re-emission of the same durable ID replaces stale metadata.
        records[artifact_id] = record
    values = list(records.values())
    _apply_artifact_validation_statuses(values, task_events)
    return values


def _new_worker_loop() -> asyncio.AbstractEventLoop:
    """Create the dedicated worker loop without changing the app-wide policy.

    ``ProactorEventLoop`` can lose an overlapped I/O completion while the
    dedicated daemon worker is handling blocking provider work on Windows
    (WinError 995 / InvalidStateError).  The selector implementation avoids
    that worker-thread failure mode; Uvicorn and all other application loops
    keep their configured defaults.
    """

    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def _ensure_worker_loop() -> asyncio.AbstractEventLoop:
    """Return the dedicated society loop, starting its daemon thread lazily."""

    global _worker_loop, _worker_thread
    with _worker_lock:
        if _worker_loop is not None and _worker_thread is not None and _worker_thread.is_alive():
            return _worker_loop
        loop = _new_worker_loop()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        thread = threading.Thread(target=run_loop, daemon=True, name="qwendom-society-worker")
        thread.start()
        _worker_loop = loop
        _worker_thread = thread
        return loop


def _schedule_background(factory, *, delay: float = _SCHEDULE_DELAY_SECONDS) -> asyncio.Task:
    """Schedule a coroutine *factory* on the event loop with strong reference.

    The wrapper task first awaits a small fairness delay (default 50 ms) so
    that Uvicorn can flush the HTTP response before any orchestration work
    begins.  The factory is not invoked until after this delay, which keeps
    the endpoint's serialisation path free of competing event-loop pressure.

    Exceptions are consumed and logged with full traceback via
    ``logger.error(..., exc_info=...)`` — never ``logger.exception`` outside
    an active except block.
    """

    async def _wrapper() -> None:
        await asyncio.sleep(delay)
        coro = factory()
        worker_future = asyncio.run_coroutine_threadsafe(coro, _ensure_worker_loop())
        try:
            await asyncio.wrap_future(worker_future)
        except asyncio.CancelledError:
            worker_future.cancel()
            raise

    task = asyncio.create_task(_wrapper())
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            safe_exc = RuntimeError(redact_provider_error(exc))
            logger.error(
                "Background task failed: %s",
                safe_exc,
                exc_info=(type(safe_exc), safe_exc, exc.__traceback__),
            )

    task.add_done_callback(_on_done)
    return task

app = FastAPI(title="Qwendom Agent Society", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    """Cancel tracked background tasks and mark running tasks interrupted.

    Graceful shutdown must not leave in-process running tasks persisted as
    ``running`` with no worker to continue them. Each tracked scheduler task
    is cancelled first, then the orchestrator emits truthful interruption
    events for any task still marked ``running``.
    """

    for task in list(_background_tasks):
        task.cancel()
    for task in list(_background_tasks):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    society.shutdown()
    global _worker_loop, _worker_thread
    with _worker_lock:
        loop = _worker_loop
        thread = _worker_thread
        _worker_loop = None
        _worker_thread = None
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None:
        thread.join(timeout=5)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "provider": settings.provider,
        "llm_enabled": settings.llm_enabled,
        "model": settings.active_model,
        "model_preflight": model_capability_preflight(settings),
    }


@app.get("/health/preflight")
async def provider_preflight() -> dict:
    """Expose provider/model readiness without revealing credentials."""

    return model_capability_preflight(settings)


@app.get("/agents")
async def agents() -> list[dict]:
    return [agent.model_dump() for agent in society.agents.values()]


@app.get("/agents/{agent_id}/memory")
async def agent_memory(agent_id: str) -> list[dict]:
    if agent_id not in society.agents and not society.get_agent_memory(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return society.get_agent_memory(agent_id)


@app.get("/agents/{agent_id}/dossier")
async def agent_dossier(agent_id: str, task_id: str | None = Query(default=None)) -> Response:
    agent = society.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    snapshot = society.reputation.snapshot().get(agent_id, {})
    reputation = snapshot.get("election_score", agent.reputation)
    events = society.list_events(task_id) if task_id else society.list_events()
    memory_records = society.get_agent_memory(agent_id)
    result = await asyncio.to_thread(
        project_dossier,
        agent=agent,
        events=events,
        memory_records=memory_records,
        reputation=reputation,
        task_id=task_id,
    )
    return Response(content=result.model_dump_json(), media_type="application/json")


@app.get("/teams")
async def teams() -> list[dict]:
    return [team.model_dump() for team in society.teams.values()]


@app.get("/metrics")
async def metrics() -> dict:
    """Return aggregate V3 task metrics for completed in-process runs."""

    return society.metrics_summary()


@app.post("/tasks")
async def create_task(request: TaskRequest) -> dict:
    """Queue a task and schedule its society run on the event loop.

    Using ``asyncio.create_task`` returns the response immediately while the
    run executes independently, preventing the client fetch from blocking on
    the orchestrator's async work.
    """
    preflight = model_capability_preflight(settings)
    if not settings.llm_enabled and not settings.allow_deterministic_no_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "No model credential is configured. Set QWEN_API_KEY for the "
                "submission path, or explicitly opt into deterministic local "
                "mode with ALLOW_DETERMINISTIC_NO_KEY=true."
            ),
        )
    if settings.llm_enabled and not preflight["ready"]:
        raise HTTPException(status_code=503, detail=preflight["reason"])
    # Keep the check and event-ledger-backed submission together so concurrent
    # HTTP requests cannot both pass the single-active-mission boundary.
    with _mission_submission_lock:
        current_tasks = getattr(society, "tasks", {})
        active_mission = next(
            (
                task
                for task in current_tasks.values()
                if getattr(task, "status", None) in _ACTIVE_MISSION_STATUSES
            ),
            None,
        ) if isinstance(current_tasks, dict) else None
        if active_mission is not None:
            raise HTTPException(
                status_code=409,
                detail="A mission is already active. Wait for it to reach a terminal outcome before submitting another.",
            )
        task = society.submit(request.prompt)
    _schedule_background(lambda: society.run_task(task.id))
    return task.model_dump()


@app.get("/tasks")
async def list_tasks() -> list[dict]:
    return society.list_task_summaries()


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = society.get_task_summary(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/clarifications")
async def clarify_task(task_id: str, request: ClarificationRequest) -> dict:
    """Apply clarification now and resume orchestration on the event loop."""

    try:
        task = society.apply_user_clarification(task_id, request.answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _schedule_background(lambda: society.continue_after_clarification(task.id))
    return task.model_dump()


@app.get("/tasks/{task_id}/events")
async def events(task_id: str) -> Response:
    task_events = society.list_events(task_id)
    if task_id not in society.tasks and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    # SocietyEvent already provides validated JSON. Joining those records
    # avoids FastAPI's costly recursive encoder on nested tool-call histories.
    payload = "[" + ",".join(event.model_dump_json() for event in task_events) + "]"
    return Response(content=payload, media_type="application/json")


def _task_artifact_record(task_id: str, artifact_id: str) -> dict[str, Any]:
    """Find one normalized artifact, preserving missing and integrity truth."""

    task_events = society.list_events(task_id)
    if society.get_task_summary(task_id) is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    record = next((item for item in _artifact_records(task_id, task_events) if item["id"] == artifact_id), None)
    if record is None or record["status"] == "missing":
        raise HTTPException(status_code=404, detail="Artifact not found")
    if record["status"] == "integrity_failed":
        raise HTTPException(status_code=409, detail="Artifact integrity check failed")
    return record


@app.get("/tasks/{task_id}/artifacts")
async def task_artifacts(task_id: str) -> list[dict[str, Any]]:
    """List durable AgentBay exports using paths safe for the browser."""

    task_events = society.list_events(task_id)
    if society.get_task_summary(task_id) is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    return [{key: value for key, value in item.items() if key != "_path"} for item in _artifact_records(task_id, task_events)]


@app.get("/tasks/{task_id}/artifacts/{artifact_id}")
async def task_artifact(task_id: str, artifact_id: str) -> FileResponse:
    """Download an integrity-verified durable artifact as an attachment."""

    record = _task_artifact_record(task_id, artifact_id)
    return FileResponse(
        record["_path"],
        filename=record["filename"],
        media_type="application/octet-stream",
        headers={"X-Artifact-SHA256": record["sha256"]},
    )


@app.get("/tasks/{task_id}/artifacts/{artifact_id}/view")
async def task_artifact_view(task_id: str, artifact_id: str) -> FileResponse:
    """Serve a safely previewable artifact inline after re-verifying its hash."""

    record = _task_artifact_record(task_id, artifact_id)
    if record["view_url"] is None:
        raise HTTPException(status_code=415, detail="Artifact type is not safely previewable")
    return FileResponse(
        record["_path"],
        filename=record["filename"],
        media_type=record["media_type"],
        content_disposition_type="inline",
        headers={"X-Artifact-SHA256": record["sha256"], "X-Content-Type-Options": "nosniff"},
    )


@app.get("/tasks/{task_id}/cockpit")
async def task_cockpit(task_id: str) -> Response:
    task_events = society.list_events(task_id)
    task = society.get_task_summary(task_id)
    if task is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    result = await asyncio.to_thread(project_cockpit, task_id, task_events, task)
    return Response(content=result.model_dump_json(), media_type="application/json")


@app.get("/tasks/{task_id}/review")
async def task_review(task_id: str) -> Response:
    task_events = society.list_events(task_id)
    if society.get_task_summary(task_id) is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    result = await asyncio.to_thread(project_review, task_id, task_events)
    return Response(content=result.model_dump_json(), media_type="application/json")


@app.get("/tasks/{task_id}/recap")
async def task_recap(task_id: str) -> Response:
    task_events = society.list_events(task_id)
    task = society.get_task_summary(task_id)
    if task is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    result = await asyncio.to_thread(project_recap, task_id, task_events, task)
    return Response(content=result.model_dump_json(), media_type="application/json")


@app.get("/tasks/{task_id}/stream")
async def stream_events(task_id: str) -> StreamingResponse:
    async def event_generator():
        existing_events = society.list_events(task_id)
        if task_id not in society.tasks and not existing_events:
            yield 'event: error\ndata: {"error":"Task not found"}\n\n'
            return

        sent = 0
        idle = 0
        max_idle_seconds = 300
        poll_interval = 0.5
        while True:
            events_for_task = society.list_events(task_id)
            new_events = events_for_task[sent:]
            if new_events:
                idle = 0
            else:
                idle += poll_interval
            for event in new_events:
                yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
            sent = len(events_for_task)
            task = society.tasks.get(task_id)
            event_status = derive_task_status(events_for_task) if events_for_task else "running"
            if event_status in {"complete", "complete_with_warnings", "failed", "interrupted"}:
                break
            if task and task.status in {"waiting_for_user", "remediation"}:
                break
            if idle >= max_idle_seconds:
                yield 'event: timeout\ndata: {"error":"Stream idle timeout"}\n\n'
                break
            await asyncio.sleep(poll_interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


frontend_dist = (
    Path(settings.frontend_dist_dir).resolve()
    if settings.frontend_dist_dir.strip()
    else (Path(__file__).resolve().parents[1] / "frontend" / "dist").resolve()
)
if frontend_dist.is_dir():
    # API routes are registered first, so this final mount serves only the
    # production UI and its static assets from the same origin.
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
