from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import threading
import unittest
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from society.models import ClarificationRequest, SocietyEvent, TaskRequest, ToolCallPayload


class ArtifactDownloadTests(unittest.IsolatedAsyncioTestCase):
    """Task artifact downloads stay hash-bound and inside the task root."""

    async def test_downloads_recorded_artifact_with_integrity_header(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "bundle" / "report.json"
            target.parent.mkdir(parents=True)
            payload = b'{"passed":true}\n'
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            event = SimpleNamespace(
                type="agentbay_artifact_exported",
                payload={
                    "workspace_relative_path": "reports/report.json",
                    "artifact_ref": {
                        "id": "artifact-report",
                        "storage_path": "bundle/report.json",
                        "sha256": digest,
                    },
                },
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [event]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                response = await main.task_artifact("task-1", "artifact-report")

            self.assertEqual(Path(response.path), target)
            self.assertEqual(response.headers["x-artifact-sha256"], digest)
            self.assertIn('filename="report.json"', response.headers["content-disposition"])

    async def test_rejects_recorded_storage_path_escape(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "task"
            root.mkdir()
            outside = Path(temp_dir) / "outside.txt"
            outside.write_text("nope", encoding="utf-8")
            event = SimpleNamespace(
                type="agentbay_artifact_exported",
                payload={
                    "artifact_ref": {
                        "id": "artifact-escape",
                        "storage_path": "../outside.txt",
                        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                    }
                },
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [event]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                with self.assertRaises(HTTPException) as raised:
                    await main.task_artifact("task-1", "artifact-escape")

            self.assertEqual(raised.exception.status_code, 404)

    async def test_lists_deduplicated_exports_and_views_safe_text_inline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "bundle" / "answer.py"
            target.parent.mkdir(parents=True)
            payload = b"print('verified')\n"
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            event = SimpleNamespace(
                type="agentbay_artifact_exported",
                payload={
                    "workspace_relative_path": "src/answer.py",
                    "role_key": "builder",
                    "artifact_ref": {"id": "artifact-answer", "type": "code", "storage_path": "bundle/answer.py", "sha256": digest, "size_bytes": len(payload)},
                },
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [event, event]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")
                response = await main.task_artifact_view("task-1", "artifact-answer")

            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["path"], "src/answer.py")
            self.assertEqual(artifacts[0]["relative_path"], "src/answer.py")
            self.assertEqual(artifacts[0]["status"], "available")
            self.assertEqual(artifacts[0]["producer"], "builder")
            self.assertEqual(artifacts[0]["validation_status"], "not_validated")
            self.assertEqual(artifacts[0]["view_url"], "/tasks/task-1/artifacts/artifact-answer/view")
            self.assertNotIn("_path", artifacts[0])
            self.assertEqual(response.headers["content-disposition"].split(";", 1)[0], "inline")
            self.assertEqual(response.media_type, "text/plain")

    async def test_lists_and_views_recorded_media_artifact(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "media" / "artifacts" / "visual.png"
            target.parent.mkdir(parents=True)
            payload = b"\x89PNG\r\n\x1a\nfixture"
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            event = SimpleNamespace(
                type="composition_media_artifact_recorded",
                payload={
                    "producer": "image_creator",
                    "expected_artifact": "launch_visual.png",
                    "artifact": {
                        "artifact_id": "artifact-image",
                        "kind": "image",
                        "local_relative_path": "media/artifacts/visual.png",
                        "sha256": digest,
                        "byte_size": len(payload),
                    },
                },
            )
            validation = SimpleNamespace(
                type="local_independent_validation_reported",
                payload={"passed": True, "inspected_artifact_refs": ["media/artifacts/visual.png"]},
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [event, validation]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")
                response = await main.task_artifact_view("task-1", "artifact-image")

            self.assertEqual(artifacts[0]["path"], "launch_visual.png")
            self.assertEqual(artifacts[0]["producer"], "image_creator")
            self.assertEqual(artifacts[0]["status"], "available")
            self.assertEqual(artifacts[0]["media_type"], "image/png")
            self.assertEqual(artifacts[0]["sha256"], digest)
            self.assertEqual(artifacts[0]["validation_status"], "passed")
            self.assertEqual(artifacts[0]["view_url"], "/tasks/task-1/artifacts/artifact-image/view")
            self.assertEqual(response.media_type, "image/png")

    async def test_list_reports_missing_export_without_exposing_storage_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            event = SimpleNamespace(
                type="agentbay_artifact_exported",
                payload={
                    "workspace_relative_path": "output/movie.mp4",
                    "artifact_ref": {"id": "artifact-missing", "type": "video", "storage_path": "private/missing.mp4", "sha256": "a" * 64},
                },
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [event]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")

            self.assertEqual(artifacts[0]["status"], "missing")
            self.assertNotIn("private/missing.mp4", str(artifacts[0]))

    async def test_hash_mismatch_is_listed_but_cannot_be_downloaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "media" / "artifacts" / "tampered.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"tampered")
            event = SimpleNamespace(
                type="composition_media_artifact_recorded",
                payload={
                    "expected_artifact": "launch_visual.png",
                    "artifact": {
                        "artifact_id": "artifact-tampered",
                        "kind": "image",
                        "local_relative_path": "media/artifacts/tampered.png",
                        "sha256": "a" * 64,
                    },
                },
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [event]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")
                with self.assertRaises(HTTPException) as raised:
                    await main.task_artifact("task-1", "artifact-tampered")

            self.assertEqual(artifacts[0]["status"], "integrity_failed")
            self.assertEqual(raised.exception.status_code, 409)
            self.assertNotIn(str(root), str(artifacts[0]))

    async def test_list_correlates_explicit_artifact_validation_evidence(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "bundle" / "report.json"
            target.parent.mkdir(parents=True)
            payload = b'{}\n'
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            export = SimpleNamespace(
                type="agentbay_artifact_exported",
                payload={"workspace_relative_path": "reports/report.json", "artifact_ref": {"id": "artifact-report", "storage_path": "bundle/report.json", "sha256": digest}},
            )
            validation = SimpleNamespace(
                type="local_independent_validation_reported",
                payload={"passed": False, "inspected_artifact_refs": ["reports/report.json"]},
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [export, validation]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")

            self.assertEqual(artifacts[0]["validation_status"], "failed")

    async def test_task_local_absolute_inspection_path_correlates_without_returning_it(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "agentbay" / "digest"
            target.parent.mkdir(parents=True)
            payload = b"verified\n"
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            export = SimpleNamespace(
                type="agentbay_artifact_exported",
                payload={"workspace_relative_path": "src/verified.py", "artifact_ref": {"id": "artifact-verified", "storage_path": "agentbay/digest", "sha256": digest}},
            )
            validation = SimpleNamespace(
                type="local_independent_validation_reported",
                payload={"passed": True, "inspected_artifact_refs": [str(target.resolve())]},
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [export, validation]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")

            self.assertEqual(artifacts[0]["validation_status"], "passed")
            self.assertNotIn(str(target.resolve()), str(artifacts[0]))

    async def test_legacy_media_manifest_is_visible_then_replaced_by_canonical_event(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "media" / "artifacts" / "visual.png"
            target.parent.mkdir(parents=True)
            payload = b"\x89PNG\r\n\x1a\nfixture"
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            legacy = SimpleNamespace(
                type="composition_tool_event",
                payload={
                    "success": True,
                    "role_key": "image_creator",
                    "data": {"artifacts": [{
                        "artifact_id": "artifact-image",
                        "kind": "image",
                        "local_relative_path": "artifacts/visual.png",
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "sha256": digest,
                    }]},
                },
            )
            canonical = SimpleNamespace(
                type="composition_media_artifact_recorded",
                payload={
                    "producer": "image_creator",
                    "expected_artifact": "launch_visual.png",
                    "artifact": {
                        "artifact_id": "artifact-image",
                        "kind": "image",
                        "local_relative_path": "media/artifacts/visual.png",
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "sha256": digest,
                        "model_id": "qwen-image",
                        "width": 1792,
                        "height": 1008,
                    },
                },
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [legacy, canonical]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")

            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["path"], "launch_visual.png")
            self.assertEqual(artifacts[0]["model_id"], "qwen-image")
            self.assertEqual((artifacts[0]["width"], artifacts[0]["height"]), (1792, 1008))

    async def test_failed_legacy_generation_remains_visible_but_never_passes_validation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "media" / "artifacts" / "partial.png"
            target.parent.mkdir(parents=True)
            payload = b"\x89PNG\r\n\x1a\npartial"
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            media = SimpleNamespace(
                type="composition_tool_event",
                payload={
                    "success": False,
                    "data": {"artifact": {
                        "artifact_id": "artifact-partial",
                        "kind": "image",
                        "local_relative_path": "artifacts/partial.png",
                        "sha256": digest,
                    }},
                },
            )
            validation = SimpleNamespace(
                type="local_independent_validation_reported",
                payload={"passed": True, "inspected_artifact_refs": ["media/artifacts/partial.png"]},
            )
            fake_society = Mock()
            fake_society.list_events.return_value = [media, validation]
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")

            self.assertEqual(artifacts[0]["status"], "available")
            self.assertEqual(artifacts[0]["generation_status"], "failed")
            self.assertEqual(artifacts[0]["validation_status"], "failed")

    async def test_ambiguous_inspected_basename_does_not_validate_multiple_exports(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            for artifact_id, relative_path, storage_path in (
                ("artifact-one", "src/one/result.txt", "one/result.txt"),
                ("artifact-two", "src/two/result.txt", "two/result.txt"),
            ):
                target = root / storage_path
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = artifact_id.encode("utf-8")
                target.write_bytes(payload)
                events.append(SimpleNamespace(
                    type="agentbay_artifact_exported",
                    payload={"workspace_relative_path": relative_path, "artifact_ref": {"id": artifact_id, "storage_path": storage_path, "sha256": hashlib.sha256(payload).hexdigest()}},
                ))
            events.append(SimpleNamespace(
                type="local_independent_validation_reported",
                payload={"passed": True, "inspected_artifact_refs": ["result.txt"]},
            ))
            fake_society = Mock()
            fake_society.list_events.return_value = events
            fake_society.get_task_summary.return_value = {"id": "task-1"}
            fake_society._composition_artifact_root.return_value = root

            with patch.object(main, "society", fake_society):
                artifacts = await main.task_artifacts("task-1")

            self.assertEqual([item["validation_status"] for item in artifacts], ["pending", "pending"])


class ToolCallPayloadTests(unittest.TestCase):
    """Structured retries use provider-neutral domain event names."""

    def test_accepts_structured_output_recovered_mode(self) -> None:
        payload = ToolCallPayload(
            tool_name="cast_readiness_vote",
            actor="architect",
            input_summary="retry",
            result={"ready": True},
            mode="structured_output_recovered",
            success=True,
        )
        self.assertEqual(payload.mode, "structured_output_recovered")

    def test_accepts_qwen_json_retry_mode(self) -> None:
        """Persisted events using the old name remain replayable."""
        payload = ToolCallPayload(
            tool_name="cast_readiness_vote",
            actor="architect",
            input_summary="retry",
            result={"ready": True},
            mode="qwen_json_retry",
            success=True,
        )
        self.assertEqual(payload.mode, "qwen_json_retry")


class TaskRequestValidationTests(unittest.TestCase):
    def test_rejects_whitespace_and_label_only_missions(self) -> None:
        for prompt in ("        ", "Mission:", " objective ", "Scope:\n\nConstraints:\n\nSuccess criteria:"):
            with self.assertRaises(ValueError):
                TaskRequest(prompt=prompt)

    def test_template_only_payload_returns_422_before_busy_check(self) -> None:
        fake_society = Mock()
        fake_society.list_task_summaries.return_value = [{"id": "task-active", "status": "running"}]

        with patch.object(main, "society", fake_society), TestClient(main.app) as client:
            response = client.post("/tasks", json={"prompt": "Scope:\n\nConstraints:\n\nSuccess criteria:"})

        self.assertEqual(response.status_code, 422)
        fake_society.list_task_summaries.assert_not_called()
        fake_society.submit.assert_not_called()

    def test_trims_valid_mission_content(self) -> None:
        request = TaskRequest(prompt="  Build a small validated API endpoint.  ")
        self.assertEqual(request.prompt, "Build a small validated API endpoint.")


class _StreamWithReconfigure:
    """Small stream double that records encoding configuration requests."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.error = error

    def reconfigure(self, **kwargs: str) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)


class Utf8StreamConfigurationTests(unittest.TestCase):
    def test_configures_supported_stream_for_utf8(self) -> None:
        stream = _StreamWithReconfigure()

        main._configure_utf8_stream(stream)

        self.assertEqual(stream.calls, [{"encoding": "utf-8", "errors": "backslashreplace"}])

    def test_ignores_stream_without_reconfigure(self) -> None:
        main._configure_utf8_stream(object())

    def test_ignores_unavailable_stream(self) -> None:
        main._configure_utf8_stream(_StreamWithReconfigure(ValueError("closed")))

    def test_ignores_oserror_from_reconfigure(self) -> None:
        stream = _StreamWithReconfigure(OSError("detached"))

        main._configure_utf8_stream(stream)

        self.assertEqual(stream.calls, [])


class BackgroundRunSchedulingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _ready_settings():
        """Return explicit provider settings so endpoint tests never depend on a local .env."""

        return main.settings.model_copy(update={
            "provider": "qwen",
            "qwen_api_key": "test-key",
            "qwen_model": "qwen3.7-plus",
            "allow_deterministic_no_key": False,
        })

    async def test_windows_worker_uses_live_selector_loop_without_policy_mutation(self) -> None:
        """The dedicated Windows worker stays responsive on a selector loop."""

        with patch.object(main.sys, "platform", "win32"):
            loop = main._new_worker_loop()
        self.assertIsInstance(loop, asyncio.SelectorEventLoop)

        started = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        worker = threading.Thread(target=run_loop, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=1))

        async def verify_liveness() -> bool:
            return True

        try:
            future = asyncio.run_coroutine_threadsafe(verify_liveness(), loop)
            self.assertTrue(await asyncio.wrap_future(future))
        finally:
            loop.call_soon_threadsafe(loop.stop)
            worker.join(timeout=1)
            loop.close()

    async def test_blocking_worker_does_not_starve_serving_loop(self) -> None:
        """A provider implementation that blocks must stay off FastAPI's loop."""

        async def blocking_coro() -> None:
            time.sleep(0.25)

        wrapper_task = main._schedule_background(blocking_coro, delay=0.01)
        await asyncio.sleep(0.04)
        started = time.perf_counter()
        await asyncio.sleep(0.02)
        elapsed = time.perf_counter() - started
        await wrapper_task

        self.assertLess(elapsed, 0.1)

    async def test_create_task_returns_before_queued_run_executes(self) -> None:
        task = SimpleNamespace(id="task-new", model_dump=lambda: {"id": "task-new", "status": "running"})
        fake_society = Mock()
        fake_society.submit.return_value = task
        fake_society.list_task_summaries.return_value = []

        run_executed = asyncio.Event()

        async def mock_run_task(task_id: str) -> None:
            run_executed.set()

        fake_society.run_task = mock_run_task

        with patch.object(main, "society", fake_society), patch.object(main, "settings", self._ready_settings()):
            response = await main.create_task(TaskRequest(prompt="A sufficiently detailed mission prompt"))

            self.assertEqual(response, {"id": "task-new", "status": "running"})
            fake_society.submit.assert_called_once_with("A sufficiently detailed mission prompt")
            self.assertFalse(run_executed.is_set())

            await asyncio.sleep(0.25)
            self.assertTrue(run_executed.is_set())

    async def test_clarification_returns_before_queued_resume_executes(self) -> None:
        task = SimpleNamespace(id="task-paused", model_dump=lambda: {"id": "task-paused", "status": "running"})
        fake_society = Mock()
        fake_society.apply_user_clarification.return_value = task

        resume_executed = asyncio.Event()

        async def mock_continue(task_id: str):
            resume_executed.set()
            return task

        fake_society.continue_after_clarification = mock_continue

        with patch.object(main, "society", fake_society):
            response = await main.clarify_task(
                "task-paused",
                ClarificationRequest(answer="Use the documented local-first boundary."),
            )

            self.assertEqual(response, {"id": "task-paused", "status": "running"})
            self.assertFalse(resume_executed.is_set())

            await asyncio.sleep(0.25)
            self.assertTrue(resume_executed.is_set())

    async def test_clarification_preserves_not_found_response(self) -> None:
        fake_society = Mock()
        fake_society.apply_user_clarification.side_effect = KeyError("missing")

        with patch.object(main, "society", fake_society):
            with self.assertRaises(HTTPException) as raised:
                await main.clarify_task(
                    "missing",
                    ClarificationRequest(answer="A valid clarification answer."),
                )

        self.assertEqual(raised.exception.status_code, 404)

    async def test_clarification_preserves_conflict_response(self) -> None:
        fake_society = Mock()
        fake_society.apply_user_clarification.side_effect = ValueError("Task is not waiting for clarification")

        with patch.object(main, "society", fake_society):
            with self.assertRaises(HTTPException) as raised:
                await main.clarify_task(
                    "task-running",
                    ClarificationRequest(answer="A valid clarification answer."),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "Task is not waiting for clarification")

    async def test_factory_not_called_when_endpoint_returns(self) -> None:
        task = SimpleNamespace(id="task-fair", model_dump=lambda: {"id": "task-fair", "status": "running"})
        fake_society = Mock()
        fake_society.submit.return_value = task
        fake_society.list_task_summaries.return_value = []
        fake_society.run_task = AsyncMock()

        with patch.object(main, "society", fake_society), patch.object(main, "settings", self._ready_settings()):
            response = await main.create_task(TaskRequest(prompt="test fairness"))

            self.assertEqual(response, {"id": "task-fair", "status": "running"})
            fake_society.run_task.assert_not_called()

            await asyncio.sleep(0.25)
            fake_society.run_task.assert_called_once_with("task-fair")

    async def test_create_task_rejects_implicit_no_key_fallback(self) -> None:
        no_key_settings = main.settings.model_copy(update={
            "provider": "qwen",
            "qwen_api_key": "",
            "allow_deterministic_no_key": False,
        })

        fake_society = Mock()
        fake_society.list_task_summaries.return_value = []
        with patch.object(main, "society", fake_society), patch.object(main, "settings", no_key_settings):
            with self.assertRaises(HTTPException) as raised:
                await main.create_task(TaskRequest(prompt="A bounded task that requires a real model"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("No model credential", raised.exception.detail)

    async def test_create_task_allows_explicit_deterministic_local_mode(self) -> None:
        task = SimpleNamespace(id="task-local", model_dump=lambda: {"id": "task-local", "status": "running"})
        fake_society = Mock()
        fake_society.submit.return_value = task
        fake_society.list_task_summaries.return_value = []
        fake_society.run_task = AsyncMock()
        local_settings = main.settings.model_copy(update={
            "provider": "qwen",
            "qwen_api_key": "",
            "allow_deterministic_no_key": True,
        })

        with patch.object(main, "society", fake_society), patch.object(main, "settings", local_settings):
            response = await main.create_task(TaskRequest(prompt="An explicit deterministic local task"))

        self.assertEqual(response["id"], "task-local")
        await asyncio.sleep(0.25)

    async def test_create_task_rejects_second_active_mission_without_submitting(self) -> None:
        fake_society = Mock()
        fake_society.tasks = {"task-active": SimpleNamespace(status="running")}

        with patch.object(main, "society", fake_society), patch.object(main, "settings", self._ready_settings()):
            with self.assertRaises(HTTPException) as raised:
                await main.create_task(TaskRequest(prompt="Build a different bounded feature."))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("already active", raised.exception.detail)
        fake_society.submit.assert_not_called()

    async def test_create_task_admits_new_work_when_only_historical_pause_is_replayed(self) -> None:
        task = SimpleNamespace(id="task-new", model_dump=lambda: {"id": "task-new", "status": "running"})
        fake_society = Mock()
        fake_society.tasks = {}
        fake_society.list_task_summaries.return_value = [{"id": "old-pause", "status": "waiting_for_user"}]
        fake_society.submit.return_value = task
        fake_society.run_task = AsyncMock()

        with patch.object(main, "society", fake_society), patch.object(main, "settings", self._ready_settings()):
            response = await main.create_task(TaskRequest(prompt="Build a fresh autonomous mission."))

        self.assertEqual(response["id"], "task-new")
        fake_society.submit.assert_called_once_with("Build a fresh autonomous mission.")
        await asyncio.sleep(0.25)

    async def test_create_task_blocks_current_queued_or_running_work(self) -> None:
        for status in ("queued", "running"):
            with self.subTest(status=status):
                fake_society = Mock()
                fake_society.tasks = {"task-active": SimpleNamespace(status=status)}

                with patch.object(main, "society", fake_society), patch.object(main, "settings", self._ready_settings()):
                    with self.assertRaises(HTTPException) as raised:
                        await main.create_task(TaskRequest(prompt="Build a different bounded feature."))

                self.assertEqual(raised.exception.status_code, 409)
                fake_society.submit.assert_not_called()

    async def test_replay_stream_ends_after_interrupted_ledger_event(self) -> None:
        interrupted = SocietyEvent(task_id="task-interrupted", type="task_interrupted", message="restart", payload={})
        fake_society = Mock()
        fake_society.tasks = {}
        fake_society.list_events.return_value = [interrupted]

        with patch.object(main, "society", fake_society):
            response = await main.stream_events("task-interrupted")
            chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(len(chunks), 1)
        self.assertIn("event: task_interrupted", chunks[0])

    async def test_factory_called_only_after_delay(self) -> None:
        factory_called = asyncio.Event()

        async def mock_coro() -> None:
            pass

        def factory():
            factory_called.set()
            return mock_coro()

        delay = 0.12
        main._schedule_background(factory, delay=delay)

        await asyncio.sleep(delay * 0.4)
        self.assertFalse(factory_called.is_set())

        await asyncio.sleep(delay)
        self.assertTrue(factory_called.is_set())

    async def test_strong_reference_held_during_execution(self) -> None:
        started = threading.Event()
        release = threading.Event()

        async def mock_coro() -> None:
            started.set()
            await asyncio.to_thread(release.wait)

        wrapper_task = main._schedule_background(lambda: mock_coro(), delay=0.01)
        self.assertIn(wrapper_task, main._background_tasks)

        await asyncio.to_thread(started.wait)
        self.assertIn(wrapper_task, main._background_tasks)

        release.set()
        await asyncio.sleep(0.05)
        self.assertNotIn(wrapper_task, main._background_tasks)

    async def test_scheduled_coroutine_exceptions_are_logged(self) -> None:
        async def failing_coro() -> None:
            raise RuntimeError("Background task exploded")

        with self.assertLogs(main.logger, level=logging.ERROR) as cm:
            wrapper_task = main._schedule_background(failing_coro, delay=0.01)
            try:
                await wrapper_task
            except RuntimeError:
                pass
            await asyncio.sleep(0.05)

        self.assertTrue(any("Background task failed" in msg for msg in cm.output))

    async def test_exception_log_includes_traceback(self) -> None:
        async def failing_coro() -> None:
            raise ValueError("specific explosion detail")

        with self.assertLogs(main.logger, level=logging.ERROR) as cm:
            wrapper_task = main._schedule_background(failing_coro, delay=0.01)
            try:
                await wrapper_task
            except ValueError:
                pass
            await asyncio.sleep(0.05)

        combined = "\n".join(cm.output)
        self.assertIn("Background task failed", combined)
        self.assertIn("specific explosion detail", combined)
        self.assertIn("Traceback", combined)


class ProjectionServingTests(unittest.IsolatedAsyncioTestCase):
    """CPU-heavy projection work must not block the FastAPI serving loop."""

    async def test_slow_cockpit_projection_does_not_starve_health(self) -> None:
        started = threading.Event()
        release = threading.Event()
        fake_society = Mock()
        fake_society.list_events.return_value = [object()]
        fake_society.get_task_summary.return_value = {"status": "complete"}
        projection = SimpleNamespace(model_dump_json=lambda: '{"status":"complete"}')

        def slow_projection(*_args):
            started.set()
            release.wait(timeout=2)
            return projection

        with patch.object(main, "society", fake_society), patch.object(main, "project_cockpit", slow_projection):
            projection_task = asyncio.create_task(main.task_cockpit("task-projection"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))

            health = await asyncio.wait_for(main.health(), timeout=0.1)
            self.assertEqual(health["status"], "ok")

            release.set()
            response = await asyncio.wait_for(projection_task, timeout=1)

        self.assertEqual(response.body, b'{"status":"complete"}')


if __name__ == "__main__":
    unittest.main()
