from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import patch, mock_open
import sys
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from society.models import SocietyEvent
from society.memory import EventStore


def _event(task_id: str, event_type: str, idx: int, payload: dict | None = None) -> SocietyEvent:
    return SocietyEvent(
        id=f"evt-{task_id}-{idx}",
        task_id=task_id,
        type=event_type,
        message=f"{event_type} #{idx}",
        payload=payload or {},
        created_at=f"2026-07-12T10:00:{idx:02d}+00:00",
    )


class ReplayTests(unittest.TestCase):
    def test_replay_restores_events_and_order(self, tmp_path: Path | None = None) -> None:
        import tempfile
        base = tmp_path or Path(tempfile.mkdtemp())
        path = base / "events.jsonl"

        e1 = _event("t1", "task_received", 1, {"prompt": "first"})
        e2 = _event("t1", "task_complete", 2, {"answer": "done"})
        e3 = _event("t2", "task_received", 3, {"prompt": "second"})

        with path.open("w", encoding="utf-8") as f:
            for e in (e1, e2, e3):
                f.write(e.model_dump_json() + "\n")

        store = EventStore(path)

        self.assertEqual(len(store.list()), 3)
        self.assertEqual(store.list("t1"), [e1, e2])
        self.assertEqual(store.list("t2"), [e3])
        self.assertEqual(store.task_ids(), ["t1", "t2"])

    def test_replay_preserves_append_order_across_tasks(self) -> None:
        import tempfile
        base = Path(tempfile.mkdtemp())
        path = base / "events.jsonl"

        events = [
            _event("a", "task_received", 1),
            _event("b", "task_received", 2),
            _event("a", "task_complete", 3),
            _event("b", "task_complete", 4),
        ]
        with path.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(e.model_dump_json() + "\n")

        store = EventStore(path)
        self.assertEqual(store.list(), events)
        self.assertEqual(store.task_ids(), ["a", "b"])


class AppendVisibilityTests(unittest.TestCase):
    def test_appended_event_visible_in_list_immediately(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        e = _event("t1", "task_received", 1, {"prompt": "hello"})
        store.append(e)

        self.assertEqual(store.list("t1"), [e])
        self.assertEqual(store.list(), [e])

    def test_appended_event_visible_in_task_ids(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("x", "task_received", 1))
        store.append(_event("y", "task_received", 2))

        self.assertEqual(store.task_ids(), ["x", "y"])


class FilteringOrderTests(unittest.TestCase):
    def test_list_by_task_returns_only_matching_events_in_order(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        e1 = _event("a", "task_received", 1)
        e2 = _event("b", "task_received", 2)
        e3 = _event("a", "task_complete", 3)
        e4 = _event("b", "task_complete", 4)
        for e in (e1, e2, e3, e4):
            store.append(e)

        self.assertEqual(store.list("a"), [e1, e3])
        self.assertEqual(store.list("b"), [e2, e4])

    def test_list_unknown_task_returns_empty(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("a", "task_received", 1))
        self.assertEqual(store.list("nonexistent"), [])

    def test_list_returns_shallow_copy(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        e = _event("a", "task_received", 1)
        store.append(e)
        result = store.list()
        result.clear()
        self.assertEqual(len(store.list()), 1)


class StatusTransitionTests(unittest.TestCase):
    def test_task_summary_running(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "do stuff"}))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "running")

    def test_task_summary_complete(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "do stuff"}))
        store.append(_event("t1", "task_complete", 2, {"answer": "done"}))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["final_answer"], "done")

    def test_task_summary_failed(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "do stuff"}))
        store.append(_event("t1", "task_failed", 2))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "failed")

    def test_task_summary_waiting_for_user(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "do stuff"}))
        store.append(_event("t1", "user_clarification_requested", 2))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "waiting_for_user")

    def test_task_summary_resume_from_waiting(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "do stuff"}))
        store.append(_event("t1", "user_clarification_requested", 2))
        store.append(_event("t1", "society_resumed", 3))
        summary = store.task_summary("t1")
        self.assertEqual(summary["status"], "running")

    def test_task_summaries_order_newest_first(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("first", "task_received", 1, {"prompt": "one"}))
        store.append(_event("second", "task_received", 2, {"prompt": "two"}))
        store.append(_event("first", "task_complete", 3))
        summaries = store.task_summaries()
        self.assertEqual([s["id"] for s in summaries], ["second", "first"])

    def test_task_summaries_status_transitions(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "p"}))
        store.append(_event("t1", "user_clarification_requested", 2))
        store.append(_event("t1", "society_resumed", 3))
        store.append(_event("t1", "task_complete", 4, {"answer": "ok"}))
        summaries = store.task_summaries()
        self.assertEqual(summaries[0]["status"], "complete")

    def test_task_summary_unknown_returns_none(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        self.assertIsNone(store.task_summary("nope"))


class DiskFailureTests(unittest.TestCase):
    def test_disk_write_failure_does_not_enter_cache(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        e = _event("t1", "task_received", 1, {"prompt": "hello"})

        original_open = Path.open

        def failing_open(self_path, *args, **kwargs):
            if "a" in (args[0] if args else kwargs.get("mode", "")):
                raise OSError("disk full")
            return original_open(self_path, *args, **kwargs)

        with patch.object(Path, "open", failing_open):
            with self.assertRaises(OSError):
                store.append(e)

        self.assertEqual(store.list(), [])
        self.assertEqual(store.task_ids(), [])
        self.assertIsNone(store.task_summary("t1"))


class ConcurrentAppendReadTests(unittest.TestCase):
    def test_concurrent_appends_and_reads(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        errors: list[Exception] = []
        read_snapshots: list[list[SocietyEvent]] = []

        def writer(task_id: str, count: int) -> None:
            try:
                for i in range(count):
                    store.append(_event(task_id, "task_received", i))
            except Exception as exc:
                errors.append(exc)

        def reader(iterations: int) -> None:
            try:
                for _ in range(iterations):
                    snap = store.list()
                    read_snapshots.append(snap)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=("a", 50)),
            threading.Thread(target=writer, args=("b", 50)),
            threading.Thread(target=reader, args=(100,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(store.list("a")), 50)
        self.assertEqual(len(store.list("b")), 50)
        self.assertEqual(len(store.list()), 100)

        for snap in read_snapshots:
            a_count = sum(1 for e in snap if e.task_id == "a")
            b_count = sum(1 for e in snap if e.task_id == "b")
            self.assertEqual(len(snap), a_count + b_count)

    def test_read_snapshot_is_consistent_during_append(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        for i in range(10):
            store.append(_event("t1", "task_received", i))

        snapshot = store.list("t1")
        self.assertEqual(len(snapshot), 10)

        store.append(_event("t1", "task_received", 10))
        self.assertEqual(len(snapshot), 10)
        self.assertEqual(len(store.list("t1")), 11)


class AgentMemoryTests(unittest.TestCase):
    def test_agent_memory_replay_from_disk(self) -> None:
        import tempfile
        base = Path(tempfile.mkdtemp())
        path = base / "events.jsonl"

        tool_event = SocietyEvent(
            id="evt-mem-1",
            task_id="t1",
            type="tool_call",
            message="memory_write #1",
            actor="agent-a",
            payload={
                "tool_name": "memory_write",
                "result": {"memory": "lesson one", "tags": ["collaboration"]},
                "mode": "append",
            },
            created_at="2026-07-12T10:00:01+00:00",
        )
        with path.open("w", encoding="utf-8") as f:
            f.write(tool_event.model_dump_json() + "\n")

        store = EventStore(path)
        records = store.agent_memory("agent-a")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task_id"], "t1")
        self.assertEqual(records[0]["memory"], "lesson one")
        self.assertEqual(records[0]["tags"], ["collaboration"])
        self.assertEqual(records[0]["mode"], "append")

    def test_agent_memory_append_visible_immediately(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")

        e1 = SocietyEvent(
            id="evt-am-1",
            task_id="t1",
            type="tool_call",
            message="memory_write #1",
            actor="agent-b",
            payload={
                "tool_name": "memory_write",
                "result": {"memory": "first lesson", "tags": ["risk"]},
            },
            created_at="2026-07-12T10:00:01+00:00",
        )
        e2 = SocietyEvent(
            id="evt-am-2",
            task_id="t1",
            type="tool_call",
            message="memory_write #2",
            actor="agent-b",
            payload={
                "tool_name": "memory_write",
                "result": {"memory": "second lesson", "tags": ["delegation"]},
            },
            created_at="2026-07-12T10:00:02+00:00",
        )
        store.append(e1)
        self.assertEqual(len(store.agent_memory("agent-b")), 1)

        store.append(e2)
        records = store.agent_memory("agent-b")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["memory"], "first lesson")
        self.assertEqual(records[1]["memory"], "second lesson")

    def test_agent_memory_ignores_other_agents_and_tools(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")

        store.append(SocietyEvent(
            id="evt-ign-1", task_id="t1", type="tool_call", message="memory_write",
            actor="agent-x",
            payload={"tool_name": "memory_write", "result": {"memory": "x memory", "tags": []}},
            created_at="2026-07-12T10:00:01+00:00",
        ))
        store.append(SocietyEvent(
            id="evt-ign-2", task_id="t1", type="tool_call", message="other tool",
            actor="agent-y",
            payload={"tool_name": "memory_lookup", "result": {"memory": "y memory"}},
            created_at="2026-07-12T10:00:02+00:00",
        ))
        store.append(SocietyEvent(
            id="evt-ign-3", task_id="t1", type="conversation_turn", message="chat",
            actor="agent-y", payload={}, created_at="2026-07-12T10:00:03+00:00",
        ))

        self.assertEqual(len(store.agent_memory("agent-x")), 1)
        self.assertEqual(len(store.agent_memory("agent-y")), 0)
        self.assertEqual(len(store.agent_memory("agent-z")), 0)


class ConcurrentSummaryReadTests(unittest.TestCase):
    def test_concurrent_task_summary_reads_during_appends(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("t1", "task_received", 1, {"prompt": "do stuff"}))

        errors: list[Exception] = []
        summary_snapshots: list[dict | None] = []

        def writer() -> None:
            try:
                for i in range(50):
                    store.append(_event("t1", "conversation_turn", 100 + i))
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(100):
                    snap = store.task_summary("t1")
                    summary_snapshots.append(snap)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for snap in summary_snapshots:
            self.assertIsNotNone(snap)
            self.assertEqual(snap["id"], "t1")
            self.assertGreaterEqual(snap["event_count"], 1)

    def test_concurrent_task_summaries_reads_during_appends(self) -> None:
        import tempfile
        store = EventStore(Path(tempfile.mkdtemp()) / "events.jsonl")
        store.append(_event("a", "task_received", 1, {"prompt": "one"}))
        store.append(_event("b", "task_received", 2, {"prompt": "two"}))

        errors: list[Exception] = []
        summaries_snapshots: list[list[dict]] = []

        def writer(task_id: str, count: int) -> None:
            try:
                for i in range(count):
                    store.append(_event(task_id, "conversation_turn", 100 + i))
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(80):
                    snap = store.task_summaries()
                    summaries_snapshots.append(snap)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=("a", 40)),
            threading.Thread(target=writer, args=("b", 40)),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for snap in summaries_snapshots:
            self.assertEqual(len(snap), 2)
            ids = {s["id"] for s in snap}
            self.assertEqual(ids, {"a", "b"})
            for s in snap:
                self.assertGreaterEqual(s["event_count"], 1)
                self.assertIn(s["status"], ("running", "complete", "complete_with_warnings", "remediation", "failed", "waiting_for_user"))


if __name__ == "__main__":
    unittest.main()
