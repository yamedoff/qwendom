from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from .models import SocietyEvent


class EventStore:
    """Append-only JSONL store used for replayable demos and simple memory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, event: SocietyEvent) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")

    def list(self, task_id: str | None = None) -> list[SocietyEvent]:
        if not self.path.exists():
            return []
        events: list[SocietyEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if task_id is None or raw.get("task_id") == task_id:
                    events.append(SocietyEvent(**raw))
        return events

    def task_ids(self) -> list[str]:
        """Return task ids in first-seen order from the append-only log."""

        seen: set[str] = set()
        ordered: list[str] = []
        for event in self.list():
            if event.task_id not in seen:
                seen.add(event.task_id)
                ordered.append(event.task_id)
        return ordered

    def task_summary(self, task_id: str) -> dict | None:
        """Reconstruct a lightweight task summary from persisted events."""

        events = self.list(task_id)
        if not events:
            return None

        first = events[0]
        last = events[-1]
        status = "running"
        if any(event.type == "task_complete" for event in events):
            status = "complete"
        elif any(event.type == "task_failed" for event in events):
            status = "failed"
        elif any(event.type == "user_clarification_requested" for event in events):
            status = "waiting_for_user"

        prompt = str(first.payload.get("prompt", "Prompt unavailable in older event log."))
        final_answer = None
        for event in reversed(events):
            if event.type == "task_complete":
                final_answer = event.payload.get("answer")
                break

        return {
            "id": task_id,
            "prompt": prompt,
            "status": status,
            "team_id": None,
            "final_answer": final_answer,
            "created_at": first.created_at,
            "updated_at": last.created_at,
            "event_count": len(events),
        }

    def task_summaries(self) -> list[dict]:
        """Return all persisted task summaries in newest-first order."""

        if not self.path.exists():
            return []

        summaries: dict[str, dict] = {}
        ordered_ids: list[str] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                task_id = raw.get("task_id")
                if not isinstance(task_id, str):
                    continue

                payload = raw.get("payload")
                if not isinstance(payload, dict):
                    payload = {}

                if task_id not in summaries:
                    ordered_ids.append(task_id)
                    summaries[task_id] = {
                        "id": task_id,
                        "prompt": str(payload.get("prompt", "Prompt unavailable in older event log.")),
                        "status": "running",
                        "team_id": None,
                        "final_answer": None,
                        "created_at": raw.get("created_at"),
                        "updated_at": raw.get("created_at"),
                        "event_count": 0,
                    }

                summary = summaries[task_id]
                summary["updated_at"] = raw.get("created_at")
                summary["event_count"] += 1
                event_type = raw.get("type")
                if event_type == "task_complete":
                    summary["status"] = "complete"
                    summary["final_answer"] = payload.get("answer")
                elif event_type == "task_failed" and summary["status"] != "complete":
                    summary["status"] = "failed"
                elif event_type == "user_clarification_requested" and summary["status"] == "running":
                    summary["status"] = "waiting_for_user"
                elif event_type == "society_resumed" and summary["status"] == "waiting_for_user":
                    summary["status"] = "running"

        return [summaries[task_id] for task_id in reversed(ordered_ids)]

    def agent_memory(self, agent_id: str) -> list[dict]:
        """Reconstruct memory-write records for one agent from tool_call events."""

        records: list[dict] = []
        for event in self.list():
            if event.type != "tool_call" or event.actor != agent_id:
                continue
            if event.payload.get("tool_name") != "memory_write":
                continue
            result = event.payload.get("result")
            if isinstance(result, dict):
                records.append(
                    {
                        "task_id": event.task_id,
                        "memory": result.get("memory"),
                        "tags": result.get("tags", []),
                        "created_at": event.created_at,
                        "mode": event.payload.get("mode"),
                    }
                )
        return records
