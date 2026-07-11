from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from config import get_settings
from society import SocietyOrchestrator
from society.models import ClarificationRequest, TaskRequest
from society.projections import project_cockpit, project_dossier, project_recap, project_review

settings = get_settings()
society = SocietyOrchestrator(settings)

# Strong references to running tasks so the GC cannot collect them mid-flight.
_background_tasks: set[asyncio.Task] = set()

app = FastAPI(title="Qwendom Agent Society", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "provider": settings.provider,
        "llm_enabled": settings.llm_enabled,
        "model": settings.active_model,
    }


@app.get("/agents")
def agents() -> list[dict]:
    return [agent.model_dump() for agent in society.agents.values()]


@app.get("/agents/{agent_id}/memory")
def agent_memory(agent_id: str) -> list[dict]:
    if agent_id not in society.agents and not society.get_agent_memory(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return society.get_agent_memory(agent_id)


@app.get("/agents/{agent_id}/dossier")
def agent_dossier(agent_id: str, task_id: str | None = Query(default=None)) -> dict:
    agent = society.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    snapshot = society.reputation.snapshot().get(agent_id, {})
    reputation = snapshot.get("election_score", agent.reputation)
    events = society.list_events(task_id) if task_id else society.list_events()
    return project_dossier(
        agent=agent,
        events=events,
        memory_records=society.get_agent_memory(agent_id),
        reputation=reputation,
        task_id=task_id,
    ).model_dump()


@app.get("/teams")
def teams() -> list[dict]:
    return [team.model_dump() for team in society.teams.values()]


@app.get("/metrics")
def metrics() -> dict:
    """Return aggregate V3 task metrics for completed in-process runs."""

    return society.metrics_summary()


@app.post("/tasks")
async def create_task(request: TaskRequest) -> dict:
    """Queue a task and schedule it on the running event loop.

    Using ``asyncio.create_task`` guarantees the coroutine is awaited, which
    avoids the subtle failure mode of passing an async function to a background
    task runner that only expects synchronous callables.
    """
    task = society.submit(request.prompt)
    bg = asyncio.create_task(society.run_task(task.id))
    _background_tasks.add(bg)
    bg.add_done_callback(_background_tasks.discard)
    return task.model_dump()


@app.get("/tasks")
def list_tasks() -> list[dict]:
    return society.list_task_summaries()


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = society.get_task_summary(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/clarifications")
async def clarify_task(task_id: str, request: ClarificationRequest) -> dict:
    """Resume a paused task with user clarification."""

    try:
        task = society.apply_user_clarification(task_id, request.answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    bg = asyncio.create_task(society.continue_after_clarification(task.id))
    _background_tasks.add(bg)
    bg.add_done_callback(_background_tasks.discard)
    return task.model_dump()


@app.get("/tasks/{task_id}/events")
def events(task_id: str) -> list[dict]:
    task_events = society.list_events(task_id)
    if task_id not in society.tasks and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    return [event.model_dump() for event in task_events]


@app.get("/tasks/{task_id}/cockpit")
def task_cockpit(task_id: str) -> dict:
    task_events = society.list_events(task_id)
    task = society.get_task_summary(task_id)
    if task is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    return project_cockpit(task_id, task_events, task).model_dump()


@app.get("/tasks/{task_id}/review")
def task_review(task_id: str) -> dict:
    task_events = society.list_events(task_id)
    if society.get_task_summary(task_id) is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    return project_review(task_id, task_events).model_dump()


@app.get("/tasks/{task_id}/recap")
def task_recap(task_id: str) -> dict:
    task_events = society.list_events(task_id)
    task = society.get_task_summary(task_id)
    if task is None and not task_events:
        raise HTTPException(status_code=404, detail="Task not found")
    return project_recap(task_id, task_events, task).model_dump()


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
            if task and task.status in {"waiting_for_user", "complete", "failed"} and sent == len(events_for_task):
                break
            if task is None and events_for_task and events_for_task[-1].type in {"task_complete", "task_failed"}:
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
