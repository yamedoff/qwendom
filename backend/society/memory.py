from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from .models import SocietyEvent, derive_task_status


class EventStore:
    """Append-only JSONL store with a thread-safe in-memory cache.

    Concurrency decisions
    ---------------------
    * A single ``threading.Lock`` guards both the event list and the
      task-id index.  The lock is held for the minimum duration needed:
      writes acquire it for the disk flush *and* the cache update so that
      readers never observe a half-appended event; reads acquire it only
      long enough to take a shallow copy of the relevant slice.
    * The JSONL file is scanned exactly once at construction time.  All
      subsequent reads are served from the in-memory cache, so a reader
      cannot block on disk I/O while the orchestration loop is appending.
    * ``append`` writes the line to disk first, then updates the cache
      under the same lock acquisition.  If the disk write raises, the
      cache is left untouched so that readers never see an event that
      was not durably persisted.
    * Returned lists are shallow copies; callers may mutate them without
      affecting the store's internal state.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._events: list[SocietyEvent] = []
        self._task_index: dict[str, list[int]] = {}
        self._task_order: list[str] = []
        self._replay_from_disk()

    def _replay_from_disk(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                event = SocietyEvent(**raw)
                idx = len(self._events)
                self._events.append(event)
                tid = event.task_id
                if tid not in self._task_index:
                    self._task_index[tid] = []
                    self._task_order.append(tid)
                self._task_index[tid].append(idx)

    def append(self, event: SocietyEvent) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
            idx = len(self._events)
            self._events.append(event)
            tid = event.task_id
            if tid not in self._task_index:
                self._task_index[tid] = []
                self._task_order.append(tid)
            self._task_index[tid].append(idx)

    def list(self, task_id: str | None = None) -> list[SocietyEvent]:
        with self._lock:
            if task_id is None:
                return list(self._events)
            indices = self._task_index.get(task_id)
            if not indices:
                return []
            return [self._events[i] for i in indices]

    def task_ids(self) -> list[str]:
        with self._lock:
            return list(self._task_order)

    def task_summary(self, task_id: str) -> dict | None:
        with self._lock:
            indices = self._task_index.get(task_id)
            if not indices:
                return None
            events = [self._events[i] for i in indices]

        first = events[0]
        last = events[-1]
        status = derive_task_status(events)

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
        with self._lock:
            events_snapshot = list(self._events)
            task_order_snapshot = list(self._task_order)

        summaries: dict[str, dict] = {}
        events_by_task: dict[str, list[SocietyEvent]] = {}
        for event in events_snapshot:
            task_id = event.task_id
            events_by_task.setdefault(task_id, []).append(event)
            if task_id not in summaries:
                summaries[task_id] = {
                    "id": task_id,
                    "prompt": str(event.payload.get("prompt", "Prompt unavailable in older event log.")),
                    "status": "running",
                    "team_id": None,
                    "final_answer": None,
                    "created_at": event.created_at,
                    "updated_at": event.created_at,
                    "event_count": 0,
                }

            summary = summaries[task_id]
            summary["updated_at"] = event.created_at
            summary["event_count"] += 1
            summary["status"] = derive_task_status(events_by_task[task_id])
            if event.type == "task_complete":
                summary["final_answer"] = event.payload.get("answer")

        return [summaries[tid] for tid in reversed(task_order_snapshot)]

    def agent_memory(self, agent_id: str) -> list[dict]:
        records: list[dict] = []
        with self._lock:
            for event in self._events:
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
