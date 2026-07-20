from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from society.memory import EventStore
from society.models import AgentProfile, SocietyAgent, SocietyEvent, TaskRun, Team
from society.orchestrator import SocietyOrchestrator


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "LLM_PROVIDER": "cerebras",
        "CEREBRAS_API_KEY": "test-key",
        "CEREBRAS_MODEL": "test-model",
        "LLM_TIMEOUT_SECONDS": 10,
        "READINESS_CONCURRENCY": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_orchestrator(settings: Settings | None = None, event_store: EventStore | None = None) -> SocietyOrchestrator:
    settings = settings or _make_settings()
    with patch("society.orchestrator.get_agno_db", return_value=None):
        with patch("society.orchestrator.get_settings", return_value=settings):
            orch = SocietyOrchestrator(settings=settings)
    if event_store is not None:
        orch.events = event_store
        orch._restore_from_events()
    return orch


def _make_agents(ids: list[str]) -> dict[str, SocietyAgent]:
    agents = {}
    for agent_id in ids:
        agents[agent_id] = SocietyAgent(
            id=agent_id,
            name=agent_id.capitalize(),
            role="Test Role",
            skills=["test"],
            profile=AgentProfile(
                values=["test"],
                communication_style="test",
                risk_tolerance="medium",
                decision_bias="test",
                default_blockers=[],
            ),
        )
    return agents


def _event(task_id: str, event_type: str, created_at: str, payload: dict | None = None) -> SocietyEvent:
    return SocietyEvent(
        task_id=task_id,
        type=event_type,
        message=f"{event_type} happened",
        payload=payload or {},
        created_at=created_at,
    )


class ReconstructedTimestampTests(unittest.TestCase):
    """Restart must preserve event-truth timestamps, not fabricate fresh ones."""

    def _build_vote_resolution_store(self) -> EventStore:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        task_id = "task-vote-ts"

        store.append(_event(task_id, "task_received", "2026-01-15T10:00:00+00:00", {"prompt": "Build X"}))
        store.append(_event(task_id, "team_formed", "2026-01-15T10:00:05+00:00", {
            "team": {"id": "team-1", "task_id": task_id, "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"},
        }))
        store.append(_event(task_id, "user_clarification_requested", "2026-01-15T10:05:00+00:00", {
            "question": "Which candidate?",
            "resume_phase": "vote_resolution",
            "proposals": {"architect": "proposal-a", "builder": "proposal-b"},
            "team_member_ids": ["architect", "builder"],
            "voter_ids": ["architect", "builder"],
            "team_leader_id": "architect",
            "tally": {"architect": 1, "builder": 1},
            "valid_candidate_ids": ["architect", "builder"],
            "task_prompt": "Build X",
        }))
        return store

    def _build_readiness_store(self) -> EventStore:
        tmp = Path(tempfile.mkdtemp())
        store = EventStore(tmp / "events.jsonl")
        task_id = "task-ready-ts"

        store.append(_event(task_id, "task_received", "2026-02-20T08:00:00+00:00", {"prompt": "Clarify scope"}))
        store.append(_event(task_id, "team_formed", "2026-02-20T08:00:10+00:00", {
            "team": {"id": "team-2", "task_id": task_id, "member_ids": ["architect", "builder"], "leader_id": "architect", "status": "active"},
        }))
        store.append(_event(task_id, "user_clarification_requested", "2026-02-20T08:10:00+00:00", {
            "question": "What is the scope?",
            "resume_phase": "post_readiness",
            "blockers": ["missing scope"],
            "open_questions": [],
        }))
        return store

    def test_vote_resolution_task_created_at_matches_first_event(self) -> None:
        store = self._build_vote_resolution_store()
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)
        orch.agents = _make_agents(["architect", "builder"])

        task_id = "task-vote-ts"
        self.assertIn(task_id, orch.tasks)
        task = orch.tasks[task_id]

        self.assertEqual(task.created_at, "2026-01-15T10:00:00+00:00")

    def test_vote_resolution_task_updated_at_matches_last_event(self) -> None:
        store = self._build_vote_resolution_store()
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)
        orch.agents = _make_agents(["architect", "builder"])

        task_id = "task-vote-ts"
        task = orch.tasks[task_id]

        self.assertEqual(task.updated_at, "2026-01-15T10:05:00+00:00")

    def test_readiness_task_created_at_matches_first_event(self) -> None:
        store = self._build_readiness_store()
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)
        orch.agents = _make_agents(["architect", "builder"])

        task_id = "task-ready-ts"
        self.assertIn(task_id, orch.tasks)
        task = orch.tasks[task_id]

        self.assertEqual(task.created_at, "2026-02-20T08:00:00+00:00")

    def test_readiness_task_updated_at_matches_last_event(self) -> None:
        store = self._build_readiness_store()
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)
        orch.agents = _make_agents(["architect", "builder"])

        task_id = "task-ready-ts"
        task = orch.tasks[task_id]

        self.assertEqual(task.updated_at, "2026-02-20T08:10:00+00:00")

    def test_reconstructed_timestamps_are_not_now(self) -> None:
        store = self._build_vote_resolution_store()
        settings = _make_settings()
        orch = _make_orchestrator(settings, store)
        orch.agents = _make_agents(["architect", "builder"])

        task = orch.tasks["task-vote-ts"]

        self.assertTrue(task.created_at.startswith("2026-01-15"))
        self.assertTrue(task.updated_at.startswith("2026-01-15"))
        self.assertNotEqual(task.created_at, task.updated_at)


class ProjectionOffloadTests(unittest.IsolatedAsyncioTestCase):
    """Blocking projection must not starve the async event loop."""

    async def test_blocking_cockpit_projection_does_not_starve_health(self) -> None:
        import main

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = {"status": "complete", "prompt": "test"}

        slow_result = Mock()
        slow_result.model_dump_json.return_value = '{"task_id":"t1","status":"complete"}'

        def blocking_project(*args, **kwargs):
            time.sleep(0.3)
            return slow_result

        with patch.object(main, "society", fake_society):
            with patch("main.project_cockpit", side_effect=blocking_project):
                cockpit_task = asyncio.create_task(main.task_cockpit("t1"))

                await asyncio.sleep(0.05)
                started = time.perf_counter()
                health_result = await main.health()
                health_elapsed = time.perf_counter() - started

                cockpit_result = await cockpit_task

        self.assertEqual(health_result["status"], "ok")
        self.assertLess(health_elapsed, 0.2)
        self.assertIsInstance(cockpit_result, main.Response)
        self.assertEqual(cockpit_result.media_type, "application/json")

    async def test_blocking_review_projection_does_not_starve_health(self) -> None:
        import main

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = {"status": "complete"}

        slow_result = Mock()
        slow_result.model_dump_json.return_value = '{"task_id":"t1"}'

        def blocking_project(*args, **kwargs):
            time.sleep(0.3)
            return slow_result

        with patch.object(main, "society", fake_society):
            with patch("main.project_review", side_effect=blocking_project):
                review_task = asyncio.create_task(main.task_review("t1"))

                await asyncio.sleep(0.05)
                started = time.perf_counter()
                health_result = await main.health()
                health_elapsed = time.perf_counter() - started

                review_result = await review_task

        self.assertEqual(health_result["status"], "ok")
        self.assertLess(health_elapsed, 0.2)
        self.assertIsInstance(review_result, main.Response)

    async def test_blocking_recap_projection_does_not_starve_health(self) -> None:
        import main

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = {"status": "complete"}

        slow_result = Mock()
        slow_result.model_dump_json.return_value = '{"task_id":"t1"}'

        def blocking_project(*args, **kwargs):
            time.sleep(0.3)
            return slow_result

        with patch.object(main, "society", fake_society):
            with patch("main.project_recap", side_effect=blocking_project):
                recap_task = asyncio.create_task(main.task_recap("t1"))

                await asyncio.sleep(0.05)
                started = time.perf_counter()
                health_result = await main.health()
                health_elapsed = time.perf_counter() - started

                recap_result = await recap_task

        self.assertEqual(health_result["status"], "ok")
        self.assertLess(health_elapsed, 0.2)
        self.assertIsInstance(recap_result, main.Response)

    async def test_blocking_dossier_projection_does_not_starve_health(self) -> None:
        import main

        agent = SocietyAgent(
            id="builder",
            name="Lin",
            role="Builder",
            skills=["build"],
            profile=AgentProfile(),
        )
        fake_society = Mock()
        fake_society.agents = {"builder": agent}
        fake_society.reputation.snapshot.return_value = {}
        fake_society.list_events.return_value = []
        fake_society.get_agent_memory.return_value = []

        slow_result = Mock()
        slow_result.model_dump_json.return_value = '{"agent":{"id":"builder"}}'

        def blocking_project(*args, **kwargs):
            time.sleep(0.3)
            return slow_result

        with patch.object(main, "society", fake_society):
            with patch("main.project_dossier", side_effect=blocking_project):
                dossier_task = asyncio.create_task(main.agent_dossier("builder"))

                await asyncio.sleep(0.05)
                started = time.perf_counter()
                health_result = await main.health()
                health_elapsed = time.perf_counter() - started

                dossier_result = await dossier_task

        self.assertEqual(health_result["status"], "ok")
        self.assertLess(health_elapsed, 0.2)
        self.assertIsInstance(dossier_result, main.Response)

    async def test_cockpit_404_preserved_with_offload(self) -> None:
        import main
        from fastapi import HTTPException

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = None

        with patch.object(main, "society", fake_society):
            with self.assertRaises(HTTPException) as raised:
                await main.task_cockpit("nonexistent")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_review_404_preserved_with_offload(self) -> None:
        import main
        from fastapi import HTTPException

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = None

        with patch.object(main, "society", fake_society):
            with self.assertRaises(HTTPException) as raised:
                await main.task_review("nonexistent")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_recap_404_preserved_with_offload(self) -> None:
        import main
        from fastapi import HTTPException

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = None

        with patch.object(main, "society", fake_society):
            with self.assertRaises(HTTPException) as raised:
                await main.task_recap("nonexistent")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_dossier_404_preserved_with_offload(self) -> None:
        import main
        from fastapi import HTTPException

        fake_society = Mock()
        fake_society.agents = {}

        with patch.object(main, "society", fake_society):
            with self.assertRaises(HTTPException) as raised:
                await main.agent_dossier("nonexistent")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_projection_response_is_pre_serialized_json(self) -> None:
        import main

        fake_society = Mock()
        fake_society.list_events.return_value = []
        fake_society.get_task_summary.return_value = {"status": "complete"}

        expected_json = '{"task_id":"t1","status":"complete","evidence_status":"missing","missing_sources":[]}'
        mock_result = Mock()
        mock_result.model_dump_json.return_value = expected_json

        with patch.object(main, "society", fake_society):
            with patch("main.project_cockpit", return_value=mock_result):
                response = await main.task_cockpit("t1")

        self.assertEqual(response.body, expected_json.encode())
        self.assertEqual(response.media_type, "application/json")
        parsed = json.loads(response.body)
        self.assertEqual(parsed["task_id"], "t1")


if __name__ == "__main__":
    unittest.main()
