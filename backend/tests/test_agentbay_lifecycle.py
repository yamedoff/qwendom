from __future__ import annotations

import asyncio
import threading

from backend.config import Settings
from backend.tests.test_agentbay_tools import FakeClient, FakeResult, make_settings
from backend.society.tools.agentbay import AgentBayTools


def make_tools(tmp_path, client=None, *, task_id="task-1"):
    client = client or FakeClient()
    return AgentBayTools(
        task_id=task_id,
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path,
        settings=make_settings(),
        client_factory=lambda _settings: client,
        clock=lambda: 1000.0,
    ), client


def test_context_manager_closes_on_success_and_exception(tmp_path):
    tools, client = make_tools(tmp_path)

    with tools.managed_execution_environment("task-1", "ok") as handle:
        assert handle
    assert client.delete_calls == ["sess-secret-123"]

    client.delete_calls.clear()
    try:
        with tools.managed_execution_environment("task-1", "boom"):
            raise RuntimeError("fail")
    except RuntimeError:
        pass
    assert client.delete_calls == ["sess-secret-123"]


def test_context_manager_closes_on_timeout_and_cancelled_error_baseexception_path(tmp_path):
    tools, client = make_tools(tmp_path)

    try:
        with tools.managed_execution_environment("task-1", "timeout"):
            raise TimeoutError("timed out")
    except TimeoutError:
        pass
    assert client.delete_calls == ["sess-secret-123"]

    client.delete_calls.clear()
    try:
        with tools.managed_execution_environment("task-1", "cancel"):
            raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    assert client.delete_calls == ["sess-secret-123"]


def test_context_manager_failed_operation_still_closes(tmp_path):
    tools, client = make_tools(tmp_path)

    with tools.managed_execution_environment("task-1", "fail command") as handle:
        client.session.command.next_result = FakeResult(success=False, request_id="req-cmd", error_message="bad command")
        result = tools.execute_command(handle, "pytest_target", {"target": "tests/test_ok.py"}, 5)

    assert result["success"] is False
    assert client.delete_calls == ["sess-secret-123"]


def test_close_idempotent_failed_delete_retry_and_close_all_best_effort(tmp_path):
    tools, client = make_tools(tmp_path)
    first = tools.start_execution_environment("task-1", "a")
    second = tools.start_execution_environment("task-1", "b")
    handle1 = first["data"]["handle"]
    handle2 = second["data"]["handle"]

    client.delete_result = FakeResult(success=False, request_id="req-del", error_message="delete failed")
    failed_close = tools.close_execution_environment(handle1)
    assert failed_close["success"] is False

    client.delete_result = FakeResult(success=True, request_id="req-del-2")
    retry_close = tools.close_execution_environment(handle1)
    assert retry_close["success"] is True
    idempotent = tools.close_execution_environment(handle1)
    assert idempotent["success"] is True
    assert idempotent["data"]["idempotent"] is True

    client.delete_result = FakeResult(success=False, request_id="req-del-3", error_message="still bad")
    results = tools.close_all()
    assert len(results) == 2
    assert any(item["success"] is False for item in results)
    assert any(item["success"] is True for item in results)
    assert handle1 in [item["data"]["handle"] for item in results if item["success"]]


def test_close_waits_for_inflight_operation(tmp_path):
    tools, client = make_tools(tmp_path)
    handle = tools.start_execution_environment("task-1", "a")["data"]["handle"]
    entered_command = threading.Event()
    release_command = threading.Event()
    original_execute = client.session.command.execute_command

    def blocking_execute(command, timeout_ms, cwd=None, envs=None):
        entered_command.set()
        release_command.wait(timeout=2)
        return original_execute(command, timeout_ms, cwd=cwd, envs=envs)

    client.session.command.execute_command = blocking_execute
    command_result: dict[str, object] = {}
    close_result: dict[str, object] = {}

    command_thread = threading.Thread(
        target=lambda: command_result.update(tools.execute_command(handle, "pytest_target", {"target": "tests/test_ok.py"}, 5))
    )
    close_thread = threading.Thread(target=lambda: close_result.update(tools.close_execution_environment(handle)))

    command_thread.start()
    assert entered_command.wait(timeout=2)
    close_thread.start()
    close_thread.join(timeout=0.1)
    assert close_thread.is_alive()
    release_command.set()
    command_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert command_result["success"] is True
    assert close_result["success"] is True
    assert client.delete_calls == ["sess-secret-123"]


def test_cancel_and_close_rejects_later_calls_and_close_all_retries_failed_delete(tmp_path):
    tools, client = make_tools(tmp_path)
    first = tools.start_execution_environment("task-1", "a")
    second = tools.start_execution_environment("task-1", "b")
    handle1 = first["data"]["handle"]
    handle2 = second["data"]["handle"]

    client.delete_result = FakeResult(success=False, request_id="req-del", error_message="delete failed")
    canceled = tools.cancel_and_close(handle1)
    assert canceled["success"] is False
    assert tools.execute_command(handle1, "pytest_target", {"target": "tests/test_ok.py"}, 5)["error_message"] == (
        "Execution environment has been cancelled"
    )

    client.delete_result = FakeResult(success=True, request_id="req-del-2")
    results = tools.close_all()

    retried = results[0]
    untouched = results[1]
    assert retried["success"] is True
    assert untouched["success"] is True
    assert client.delete_calls.count("sess-secret-123") == 3


def test_failed_delete_retry_after_close_exception(tmp_path):
    tools, client = make_tools(tmp_path)
    handle = tools.start_execution_environment("task-1", "a")["data"]["handle"]
    calls = {"count": 0}

    def flaky_delete(session):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return FakeResult(success=True, request_id="req-del-2")

    client.delete = flaky_delete

    failed = tools.close_execution_environment(handle)
    retried = tools.close_execution_environment(handle)

    assert failed["success"] is False
    assert retried["success"] is True
    assert calls["count"] == 2


def test_orphan_reconciliation_deletes_only_labeled_expired_or_orphaned_sessions(tmp_path):
    client = FakeClient()
    client.list_result = FakeResult(
        sessions=[
            {
                "session_id": "sess-old",
                "labels": {"managed_by": "qwendom", "task_id": "orphan", "created_at": "800"},
            },
            {
                "session_id": "sess-active",
                "labels": {"managed_by": "qwendom", "task_id": "task-1", "created_at": "995"},
            },
            {
                "session_id": "sess-unlabeled",
                "labels": {"task_id": "nope", "created_at": "800"},
            },
        ]
    )

    class LocalSession:
        def __init__(self, session_id):
            self.session_id = session_id

    client.get = lambda session_id: FakeResult(session=LocalSession(session_id), request_id=f"get-{session_id}")
    deleted = []
    client.delete = lambda session: deleted.append(session.session_id) or FakeResult(request_id=f"del-{session.session_id}")

    results = AgentBayTools.reconcile_orphaned_sessions(
        settings=make_settings(),
        active_task_ids={"task-1"},
        artifact_root=tmp_path,
        client_factory=lambda _settings: client,
        now=1000.0,
    )

    assert deleted == ["sess-old"]
    assert len(results) == 1
    assert results[0]["success"] is True
    assert results[0]["data"]["orphaned"] is True


def test_reconcile_respects_expired_active_sessions_and_response_shape_variance(tmp_path):
    client = FakeClient()
    client.list_result = FakeResult(
        sessions=[
            {
                "session_id": "sess-expired",
                "labels": {"managed_by": "qwendom", "task_id": "task-1", "created_at": "100"},
            }
        ]
    )

    class LocalSession:
        def __init__(self, session_id):
            self.session_id = session_id

    client.get_result = {"success": True, "session": LocalSession("sess-expired")}
    client.delete_result = True

    results = AgentBayTools.reconcile_orphaned_sessions(
        settings=make_settings(),
        active_task_ids={"task-1"},
        artifact_root=tmp_path,
        client_factory=lambda _settings: client,
        now=1000.0,
    )

    assert results[0]["success"] is True
    assert results[0]["data"]["expired"] is True
