from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

from backend.config import Settings


def _load_agentbay_module():
    root = Path(__file__).resolve().parents[2]
    society_dir = root / "backend" / "society"
    tools_dir = society_dir / "tools"

    backend_pkg = sys.modules.setdefault("backend", types.ModuleType("backend"))
    backend_pkg.__path__ = [str(root / "backend")]

    society_pkg = sys.modules.setdefault("backend.society", types.ModuleType("backend.society"))
    society_pkg.__path__ = [str(society_dir)]

    tools_pkg = sys.modules.setdefault("backend.society.tools", types.ModuleType("backend.society.tools"))
    tools_pkg.__path__ = [str(tools_dir)]

    for module_name, module_path in (
        ("backend.society.capability_registry", society_dir / "capability_registry.py"),
        ("backend.society.error_taxonomy", society_dir / "error_taxonomy.py"),
        ("backend.society.tools.agentbay", tools_dir / "agentbay.py"),
    ):
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules["backend.society.tools.agentbay"]


agentbay_module = _load_agentbay_module()
AgentBayTools = agentbay_module.AgentBayTools
CommandTemplate = agentbay_module.CommandTemplate
MAX_ARTIFACTS_PER_HANDLE = agentbay_module.MAX_ARTIFACTS_PER_HANDLE
MAX_ARTIFACT_SIZE_BYTES = agentbay_module.MAX_ARTIFACT_SIZE_BYTES
MAX_CODE_LENGTH = agentbay_module.MAX_CODE_LENGTH
MAX_COMMAND_OUTPUT_LENGTH = agentbay_module.MAX_COMMAND_OUTPUT_LENGTH
MAX_COMMAND_TIMEOUT_SECONDS = agentbay_module.MAX_COMMAND_TIMEOUT_SECONDS
MAX_READ_LENGTH = agentbay_module.MAX_READ_LENGTH
MAX_WRITE_LENGTH = agentbay_module.MAX_WRITE_LENGTH


class FakeResult:
    def __init__(self, success=True, request_id="req-1", error_message="", **kwargs):
        self.success = success
        self.request_id = request_id
        self.error_message = error_message
        for key, value in kwargs.items():
            setattr(self, key, value)


class DirectoryListResultLike:
    def __init__(self, entries, success=True, request_id="req-1", error_message=""):
        self.success = success
        self.request_id = request_id
        self.error_message = error_message
        self._entries = list(entries)

    @property
    def entries(self):
        return [DirectoryEntryLike(entry) for entry in self._entries]


class DirectoryEntryLike:
    def __init__(self, entry):
        self._entry = dict(entry)

    @property
    def name(self):
        return self._entry.get("name", "")

    @property
    def is_file(self):
        return self._entry.get("isFile", self._entry.get("is_file", False))

    @property
    def is_directory(self):
        return self._entry.get("is_directory", self._entry.get("isDirectory", False))

    @property
    def size(self):
        return self._entry.get("size", 0)


class FakeCommandApi:
    def __init__(self):
        self.calls = []
        self.next_result = FakeResult(stdout="ok", stderr="", exit_code=0)

    def execute_command(self, command, timeout_ms, cwd=None, envs=None):
        self.calls.append({"command": command, "timeout_ms": timeout_ms, "cwd": cwd, "envs": envs})
        return self.next_result


class FakeCodeApi:
    def __init__(self):
        self.calls = []
        self.next_result = FakeResult(output="code-ok")

    def run_code(self, code, language, timeout_s):
        self.calls.append({"code": code, "language": language, "timeout_s": timeout_s})
        return self.next_result


class FakeFileSystemApi:
    def __init__(self):
        self.files = {"/workspace/app.txt": "hello world", "/workspace/build.log": "line1\nline2\n"}
        self.file_bytes = {path: content.encode("utf-8") for path, content in self.files.items()}
        self.directories = {"/workspace", "/workspace/subdir"}
        self.listing = [
            {"path": "/workspace/app.txt", "type": "file"},
            {"path": "/workspace/subdir", "type": "directory"},
        ]
        self.write_calls = []
        self.create_directory_calls = []
        self.upload_calls = []
        self.read_calls = []
        self.list_calls = []
        self.download_calls = []
        self.write_result = True
        self.create_directory_result = True
        self.upload_result = True
        self.list_result = None
        self.before_download = None
        self.before_list = None

    def read_file(self, path, format="text"):
        self.read_calls.append({"path": path, "format": format})
        raw = self.file_bytes.get(path)
        if raw is None and path in self.files:
            raw = self.files[path].encode("utf-8")
            self.file_bytes[path] = raw
        return FakeResult(content=raw.decode("utf-8"))

    def create_directory(self, path):
        self.create_directory_calls.append({"path": path})
        self.directories.add(path)
        return self.create_directory_result

    def write_file(self, path, content, mode):
        self.write_calls.append({"path": path, "content": content, "mode": mode})
        if mode == "append":
            self.files[path] = self.files.get(path, "") + content
        else:
            self.files[path] = content
        self.file_bytes[path] = self.files[path].encode("utf-8")
        return self.write_result

    def upload_file(self, local_path, remote_path):
        self.upload_calls.append({"local_path": local_path, "remote_path": remote_path})
        raw = Path(local_path).read_bytes()
        self.file_bytes[remote_path] = raw
        try:
            self.files[remote_path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.files.pop(remote_path, None)
        return self.upload_result

    def list_directory(self, path):
        self.list_calls.append({"path": path})
        if self.before_list is not None:
            self.before_list(path)
        if self.list_result is not None:
            return self.list_result
        return FakeResult(entries=list(self.listing))

    def download_file(self, remote_path, local_path):
        self.download_calls.append({"remote_path": remote_path, "local_path": local_path})
        if self.before_download is not None:
            self.before_download(remote_path, local_path)
        if remote_path == "/workspace/fail.bin":
            Path(local_path).write_text("partial", encoding="utf-8")
            raise RuntimeError("download boom")
        raw = self.file_bytes.get(remote_path)
        if raw is None and remote_path in self.files:
            raw = self.files[remote_path].encode("utf-8")
            self.file_bytes[remote_path] = raw
        Path(local_path).write_bytes(raw)
        return FakeResult()


class FakeSession:
    def __init__(self, session_id="sess-secret-123"):
        self.session_id = session_id
        self.command = FakeCommandApi()
        self.code = FakeCodeApi()
        self.file_system = FakeFileSystemApi()


class FakeClient:
    def __init__(self):
        self.create_calls = 0
        self.delete_calls = []
        self.list_calls = []
        self.get_calls = []
        self.session = FakeSession()
        self.create_result = FakeResult(session=self.session)
        self.delete_result = FakeResult()
        self.list_result = FakeResult(sessions=[])
        self.get_result = FakeResult(session=self.session)

    def create(self, params):
        self.create_calls += 1
        self.last_params = params
        return self.create_result

    def delete(self, session):
        self.delete_calls.append(session.session_id)
        return self.delete_result

    def list(self, labels=None, page=None, limit=None, status=None, image_id=None):
        self.list_calls.append({"labels": labels, "page": page, "limit": limit})
        return self.list_result

    def get(self, session_id):
        self.get_calls.append(session_id)
        return self.get_result


def make_settings(**overrides):
    data = {
        "AGENTBAY_API_KEY": "api-key-secret",
        "AGENTBAY_ENDPOINT": "example.test",
        "AGENTBAY_REGION_ID": "region-x",
        "AGENTBAY_IMAGE_ID": "code_latest",
        "AGENTBAY_BROWSER_IMAGE_ID": "browser_latest",
        "AGENTBAY_SESSION_TIMEOUT_SECONDS": 60,
    }
    data.update(overrides)
    return Settings(**data)


def make_tools(
    tmp_path,
    client=None,
    *,
    role_key="builder",
    task_id="task-1",
    event_sink=None,
    command_templates=None,
    extra_allowed_tool_ids=None,
    workspace_source_dir=None,
    workspace_overlay_exports=None,
):
    client = client or FakeClient()
    factory_calls = {"count": 0}

    def client_factory(_settings):
        factory_calls["count"] += 1
        return client

    tools = AgentBayTools(
        task_id=task_id,
        role_key=role_key,
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path,
        settings=make_settings(),
        client_factory=client_factory,
        event_sink=event_sink,
        clock=lambda: 1000.0,
        command_templates=command_templates,
        extra_allowed_tool_ids=extra_allowed_tool_ids,
        workspace_source_dir=workspace_source_dir,
        workspace_overlay_exports=workspace_overlay_exports,
    )
    return tools, client, factory_calls


def start_handle(tools):
    result = tools.start_execution_environment("task-1", "run tests")
    assert result["success"] is True
    return result["data"]["handle"]


def test_frontend_uses_browser_capable_agentbay_image(tmp_path):
    tools, client, _events = make_tools(tmp_path, role_key="frontend_engineer")

    start_handle(tools)

    assert client.last_params.image_id == "browser_latest"


def test_registered_public_tools_exact(tmp_path):
    tools, _, _ = make_tools(tmp_path)
    assert sorted(tools.functions) == sorted(
        [
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "write_text_file",
            "list_files",
            "browser_render",
            "export_artifact",
            "close_execution_environment",
        ]
    )


def test_missing_credential_unauthorized_role_and_task_mismatch_reject_before_client_factory(tmp_path):
    client = FakeClient()
    calls = {"count": 0}

    def client_factory(_settings):
        calls["count"] += 1
        return client

    missing_key_tools = AgentBayTools(
        task_id="task-1",
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path,
        settings=make_settings(AGENTBAY_API_KEY=""),
        client_factory=client_factory,
    )
    unauthorized_tools = AgentBayTools(
        task_id="task-1",
        role_key="critic",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path,
        settings=make_settings(),
        client_factory=client_factory,
    )
    mismatch_tools = AgentBayTools(
        task_id="task-1",
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path,
        settings=make_settings(),
        client_factory=client_factory,
    )

    assert missing_key_tools.start_execution_environment("task-1", "x")["success"] is False
    assert unauthorized_tools.start_execution_environment("task-1", "x")["success"] is False
    assert mismatch_tools.start_execution_environment("task-2", "x")["success"] is False
    assert calls["count"] == 0


def test_handles_are_opaque_and_public_output_and_audit_are_redacted(tmp_path):
    events = []
    tools, client, _ = make_tools(tmp_path, event_sink=events.append)
    client.session.command.next_result = FakeResult(stdout="api-key-secret and sess-secret-123", stderr="")

    started = tools.start_execution_environment("task-1", "secret use")
    handle = started["data"]["handle"]
    assert handle != client.session.session_id
    assert "sess-secret-123" not in str(started)
    assert "api-key-secret" not in str(started)

    command = tools.execute_command(handle, "pytest_target", {"target": "tests/test_ok.py"}, 5)
    assert "api-key-secret" not in command["output"]
    assert "sess-secret-123" not in command["output"]
    assert all("api-key-secret" not in str(event) for event in events)
    assert all("sess-secret-123" not in str(event) for event in events)


def test_command_allowlist_validation_and_metacharacter_rejection_happens_before_sdk(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    unknown = tools.execute_command(handle, "unknown", {}, 5)
    traversal = tools.execute_command(handle, "pytest_target", {"target": "../bad.py"}, 5)
    metachar = tools.execute_command(handle, "pytest_target", {"target": "bad;rm"}, 5)
    timeout = tools.execute_command(handle, "pytest_target", {"target": "ok.py"}, MAX_COMMAND_TIMEOUT_SECONDS + 1)

    assert unknown["success"] is False
    assert traversal["success"] is False
    assert metachar["success"] is False
    assert timeout["success"] is False
    assert client.session.command.calls == []


def test_execute_command_uses_injected_templates_and_bounds_output(tmp_path):
    template = CommandTemplate(
        command_id="safe_cmd",
        argument_names=("target",),
        validators={"target": lambda value: value if value == "pkg/tests.py" else (_ for _ in ()).throw(ValueError("bad target"))},
        render=lambda args: ("python", "-m", "pytest", args["target"]),
    )
    tools, client, _ = make_tools(tmp_path, command_templates={"safe_cmd": template})
    handle = start_handle(tools)
    client.session.command.next_result = FakeResult(stdout="x" * (MAX_COMMAND_OUTPUT_LENGTH + 50), stderr="", exit_code=0)

    result = tools.execute_command(handle, "safe_cmd", {"target": "pkg/tests.py"}, 3)

    assert result["success"] is True
    assert client.session.command.calls[0]["command"] == "python -m pytest pkg/tests.py"
    assert len(result["output"]) <= MAX_COMMAND_OUTPUT_LENGTH + 20


def test_execute_command_emits_nonzero_exit_details_without_exposing_raw_result(tmp_path):
    events = []
    tools, client, _ = make_tools(tmp_path, event_sink=events.append)
    handle = start_handle(tools)
    client.session.command.next_result = FakeResult(
        success=False,
        error_message="",
        stdout="",
        stderr="python3: not found",
        output="python3: not found",
        exit_code=127,
    )

    result = tools.execute_command(handle, "python_compile", {"target": "/workspace/repo"}, 5)

    assert result["success"] is False
    assert result["error_code"] == "agentbay_command_failed"
    assert result["error_message"] == "python3: not found"
    failed = [event for event in events if event["event_type"] == "agentbay_command_failed"]
    assert len(failed) == 1
    assert failed[0]["command_id"] == "python_compile"
    assert failed[0]["exit_code"] == 127
    assert failed[0]["error"] == "python3: not found"


def test_default_command_templates_use_linux_python3(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    result = tools.execute_command(handle, "python_compile", {"target": "/workspace/repo"}, 5)

    assert result["success"] is True
    assert client.session.command.calls[0]["command"] == "env PYTHONPATH=/workspace python3 -m compileall /workspace/repo"


@pytest.mark.parametrize(
    ("command_id", "relative_target", "expected_command"),
    [
        (
            "pytest_target",
            "repo/test_payment_retry.py",
            "env PYTHONPATH=/workspace python3 -m pytest /workspace/repo/test_payment_retry.py -q",
        ),
        (
            "python_compile",
            "repo/payment_retry.py",
            "env PYTHONPATH=/workspace python3 -m compileall /workspace/repo/payment_retry.py",
        ),
    ],
)
def test_default_command_templates_canonicalize_workspace_targets(
    tmp_path, command_id, relative_target, expected_command
):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    relative_result = tools.execute_command(handle, command_id, {"target": relative_target}, 5)
    absolute_result = tools.execute_command(handle, command_id, {"target": f"/workspace/{relative_target}"}, 5)

    assert relative_result["success"] is True
    assert absolute_result["success"] is True
    assert [call["command"] for call in client.session.command.calls] == [expected_command, expected_command]


@pytest.mark.parametrize(
    ("target", "contract_message"),
    [
        ("../bad.py", "non-traversing"),
        ("/workspace/repo/../bad.py", "non-traversing"),
        ("/tmp/bad.py", "beneath /workspace"),
        ("/workspace-escape/bad.py", "beneath /workspace"),
        ("repo/test.py;id", "unsupported path characters"),
        ("repo/-q", "option-like"),
        ("C:\\workspace\\repo\\test.py", "relative path"),
    ],
)
def test_default_command_templates_reject_unsafe_workspace_targets(tmp_path, target, contract_message):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    result = tools.execute_command(handle, "pytest_target", {"target": target}, 5)

    assert result["success"] is False
    assert result["error_code"] == "agentbay_command_rejected"
    assert contract_message in result["error_message"]
    assert client.session.command.calls == []


def test_dependency_export_overlays_original_workspace_before_downstream_execution(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "repo.py").write_bytes(b"value = 'original'\n")
    durable = tmp_path / "producer" / "repo.py"
    durable.parent.mkdir()
    durable.write_bytes(b"value = 'repaired'\n")
    events = []
    tools, client, _ = make_tools(
        tmp_path,
        event_sink=events.append,
        workspace_source_dir=source,
        workspace_overlay_exports={"repo.py": str(durable)},
    )

    start_handle(tools)

    assert client.session.file_system.files["/workspace/repo.py"] == "value = 'repaired'\n"
    overlay_events = [event for event in events if event["event_type"] == "agentbay_workspace_overlay_succeeded"]
    assert len(overlay_events) == 1
    assert overlay_events[0]["manifest"][0]["path"] == "repo.py"


def test_run_code_enforces_language_code_and_timeout_bounds(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    assert tools.run_code(handle, "ruby", "puts 1", 5)["success"] is False
    assert tools.run_code(handle, "python", "x" * (MAX_CODE_LENGTH + 1), 5)["success"] is False
    assert tools.run_code(handle, "python", "print(1)", 0)["success"] is False
    assert client.session.code.calls == []

    ok = tools.run_code(handle, "Python", "print(1)", 5)
    assert ok["success"] is True
    assert client.session.code.calls[0]["language"] == "python"


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("python", "import json\nimport statistics\nfrom collections import Counter\nprint(json.dumps({'mean': statistics.mean([2, 4, 6]), 'count': Counter([1, 1, 2])[1]}))"),
        ("javascript", "const total = [1, 2, 3].reduce((sum, value) => sum + value, 0);"),
        ("javascript", "const values = [10, 20, 30]; const index = 1; console.log(values[index]);"),
    ],
)
def test_run_code_safe_sources_reach_sdk(tmp_path, language, code):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    result = tools.run_code(handle, language, code, 5)

    assert result["success"] is True
    assert len(client.session.code.calls) == 1
    assert client.session.code.calls[0]["code"] == code


@pytest.mark.parametrize(
    ("language", "code", "reason"),
    [
        ("python", "import os\nprint('x')", "restricted module"),
        ("python", "open('x')", "restricted capability"),
        ("python", "import asyncio\nasyncio.subprocess.create_subprocess_exec('echo')", "async subprocess"),
        ("javascript", "const cp = require('child_process');", "restricted capability"),
        ("javascript", "import fs from 'fs';", "restricted capability"),
        ("javascript", "process.binding('fs')", "restricted capability"),
        ("javascript", "eval('1 + 1')", "restricted capability"),
    ],
)
def test_run_code_rejects_dangerous_families_before_sdk(tmp_path, language, code, reason):
    events = []
    tools, client, _ = make_tools(tmp_path, event_sink=events.append)
    handle = start_handle(tools)

    result = tools.run_code(handle, language, code, 5)

    assert result["success"] is False
    assert result["error_code"] == "code_policy_violation"
    assert result["error_category"] == "capability"
    assert code not in (result["error_message"] or "")
    assert len(client.session.code.calls) == 0
    assert all(code not in str(event) for event in events)
    assert reason.split()[0] in (result["error_message"] or "").lower()


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("python", "getattr(__builtins__, 'open')"),
        ("python", "globals()['__builtins__']"),
        ("python", "import builtins"),
        ("python", "import importlib"),
        ("python", "import sys"),
        ("javascript", "globalThis['ev'+'al']"),
        ("javascript", "process['binding']"),
        ("javascript", "[]['filter']['constructor']"),
    ],
)
def test_run_code_rejects_confirmed_policy_bypasses_before_sdk(tmp_path, language, code):
    events = []
    tools, client, _ = make_tools(tmp_path, event_sink=events.append)
    handle = start_handle(tools)

    result = tools.run_code(handle, language, code, 5)

    assert result["success"] is False
    assert result["error_code"] == "code_policy_violation"
    assert result["error_category"] == "capability"
    assert code not in (result["error_message"] or "")
    assert len(client.session.code.calls) == 0
    assert all(code not in str(event) for event in events)


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("python", "def broken(:\n    pass"),
        ("javascript", "const broken = (1 + 2;"),
    ],
)
def test_run_code_rejects_syntax_errors_before_sdk(tmp_path, language, code):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    result = tools.run_code(handle, language, code, 5)

    assert result["success"] is False
    assert result["error_code"] == "code_policy_violation"
    assert "syntax" in result["error_message"].lower()
    assert client.session.code.calls == []


def test_file_path_content_and_listing_bounds(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    bad_read = tools.read_text_file(handle, "/etc/passwd", 0, 10)
    bad_write = tools.write_text_file(handle, "/workspace/out.txt", "x" * (MAX_WRITE_LENGTH + 1), "overwrite")
    bad_len = tools.read_text_file(handle, "/workspace/app.txt", 0, MAX_READ_LENGTH + 1)
    assert bad_read["success"] is False
    assert bad_write["success"] is False
    assert bad_len["success"] is False

    read_ok = tools.read_text_file(handle, "/workspace/app.txt", 6, 5)
    write_ok = tools.write_text_file(handle, "/workspace/out.txt", "content", "append")
    list_ok = tools.list_files(handle, "/workspace")

    assert read_ok["data"]["content"] == "world"
    assert write_ok["success"] is True
    assert list_ok["success"] is True
    assert client.session.file_system.write_calls[0]["mode"] == "append"


def test_write_text_file_creates_validated_parent_directory_before_nested_write(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)

    result = tools.write_text_file(
        handle,
        "/workspace/reports/test_report.json",
        '{"passed": true}',
        "overwrite",
    )

    assert result["success"] is True
    assert client.session.file_system.create_directory_calls[-1] == {"path": "/workspace/reports"}
    assert client.session.file_system.write_calls[-1]["path"] == "/workspace/reports/test_report.json"


def test_write_text_file_fails_closed_when_parent_directory_creation_fails(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    client.session.file_system.create_directory_result = FakeResult(success=False, error_message="mkdir denied")
    client.session.file_system.write_result = FakeResult(success=False, error_message="parent missing")

    result = tools.write_text_file(handle, "/workspace/reports/test_report.json", "{}", "overwrite")

    assert result["success"] is False
    assert result["error_code"] == "agentbay_write_parent_failed"
    assert len(client.session.file_system.write_calls) == 1


def test_write_text_file_treats_existing_parent_create_failure_as_nonfatal(tmp_path):
    events = []
    tools, client, _ = make_tools(tmp_path, event_sink=events.append)
    handle = start_handle(tools)
    client.session.file_system.create_directory_result = FakeResult(
        success=False,
        error_message="directory already exists",
    )

    result = tools.write_text_file(handle, "/workspace/repo/services/api/auth.py", "fixed = True\n", "overwrite")

    assert result["success"] is True
    assert client.session.file_system.files["/workspace/repo/services/api/auth.py"] == "fixed = True\n"
    assert any(event["event_type"] == "agentbay_parent_create_nonfatal" for event in events)


def test_list_files_supports_public_entries_property_backed_by_private_sdk_storage(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    client.session.file_system.list_result = DirectoryListResultLike(
        [{"name": "phase0_smoke.txt", "isFile": True, "size": 19}]
    )

    result = tools.list_files(handle, "/workspace")

    assert result["success"] is True
    assert result["data"]["entries"] == [{"path": "/workspace/phase0_smoke.txt", "type": "file"}]


def test_artifact_export_checksum_durability_and_partial_cleanup(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    client.session.file_system.files["/workspace/report.txt"] = "artifact data"
    client.session.file_system.listing.append({"path": "/workspace/report.txt", "type": "file", "size": len("artifact data".encode("utf-8"))})
    client.session.file_system.listing.append({"path": "/workspace/fail.bin", "type": "file", "size": 7})

    exported = tools.export_artifact(handle, "/workspace/report.txt", "report")
    assert exported["success"] is True
    artifact = exported["artifact_references"][0]
    assert Path(artifact["path"]).exists()
    assert artifact["size_bytes"] == len("artifact data".encode("utf-8"))
    assert len(artifact["sha256"]) == 64
    assert exported["data"]["size_preflighted"] is True

    failed = tools.export_artifact(handle, "/workspace/fail.bin", "report")
    assert failed["success"] is False
    assert failed["data"]["size_preflighted"] is True
    assert not list(tmp_path.rglob("*.part"))


def test_artifact_count_limit_and_sdk_write_shape_normalization(tmp_path):
    events = []
    tools, client, _ = make_tools(tmp_path, event_sink=events.append)
    handle = start_handle(tools)
    client.session.file_system.files["/workspace/item.txt"] = "x"
    client.session.file_system.listing.append({"path": "/workspace/item.txt", "type": "file", "size": 1})

    for _ in range(MAX_ARTIFACTS_PER_HANDLE):
        result = tools.export_artifact(handle, "/workspace/item.txt", "log")
        assert result["success"] is True
    assert tools.export_artifact(handle, "/workspace/item.txt", "log")["success"] is False

    client.session.file_system.write_result = FakeResult(success=True, request_id="req-write")
    write_result = tools.write_text_file(handle, "/workspace/write.txt", "abc", "overwrite")
    assert write_result["success"] is True
    assert write_result["request_id"] == "req-write"
    assert any(event["event_type"] == "agentbay_write_succeeded" and event["request_id"] == "req-write" for event in events)
    assert any(event["event_type"] == "agentbay_artifact_exported" for event in events)


def test_artifact_preflight_rejects_known_oversize_before_download(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    client.session.file_system.files["/workspace/huge.bin"] = "x"
    client.session.file_system.list_result = FakeResult(
        entries=[types.SimpleNamespace(name="huge.bin", path="/workspace/huge.bin", size=MAX_ARTIFACT_SIZE_BYTES + 1)]
    )

    result = tools.export_artifact(handle, "/workspace/huge.bin", "report")

    assert result["success"] is False
    assert result["data"]["size_preflighted"] is True
    assert client.session.file_system.download_calls == []


def test_export_artifact_supports_public_entries_property_backed_by_private_sdk_storage(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    client.session.file_system.files["/workspace/report.txt"] = "artifact data"
    client.session.file_system.list_result = DirectoryListResultLike(
        [{"name": "report.txt", "isFile": True, "size": 13}]
    )

    result = tools.export_artifact(handle, "/workspace/report.txt", "report")

    assert result["success"] is True
    artifact = result["artifact_references"][0]
    assert Path(artifact["path"]).exists()
    assert artifact["sha256"] == "18eec0cf867e893de3351c2efc679c3b79ceed8a9037eec96db06a50bfd718d3"
    assert result["data"]["size_preflighted"] is True


def test_artifact_unknown_size_falls_back_to_post_download_cleanup(tmp_path):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    payload = "x" * (MAX_ARTIFACT_SIZE_BYTES + 1)
    client.session.file_system.files["/workspace/unknown.bin"] = payload
    client.session.file_system.list_result = FakeResult(entries=[{"name": "unknown.bin", "path": "/workspace/unknown.bin"}])

    result = tools.export_artifact(handle, "/workspace/unknown.bin", "report")

    assert result["success"] is False
    assert result["data"]["size_preflighted"] is False
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*unknown.bin"))


def test_concurrent_artifact_exports_reserve_atomically_under_handle_lock(tmp_path, monkeypatch):
    tools, client, _ = make_tools(tmp_path)
    handle = start_handle(tools)
    client.session.file_system.files["/workspace/item.txt"] = "x"
    client.session.file_system.list_result = FakeResult(entries=[{"name": "item.txt", "path": "/workspace/item.txt", "size": 1}])
    monkeypatch.setattr(agentbay_module, "MAX_ARTIFACTS_PER_HANDLE", 1)
    entered_download = threading.Event()
    release_download = threading.Event()

    def before_download(_remote_path, _local_path):
        entered_download.set()
        release_download.wait(timeout=2)

    client.session.file_system.before_download = before_download
    results = [None, None]

    def export_in_thread(index: int) -> None:
        results[index] = tools.export_artifact(handle, "/workspace/item.txt", "log")

    first = threading.Thread(target=export_in_thread, args=(0,))
    second = threading.Thread(target=export_in_thread, args=(1,))
    first.start()
    assert entered_download.wait(timeout=2)
    second.start()
    release_download.set()
    first.join(timeout=2)
    second.join(timeout=2)

    successes = [item for item in results if item and item["success"]]
    failures = [item for item in results if item and not item["success"]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error_message"] == "Artifact export limit reached for this handle"
    assert len(client.session.file_system.download_calls) == 1


def test_recursive_redaction_masks_nested_secrets_and_does_not_mutate_inputs(tmp_path):
    tools = AgentBayTools(
        task_id="task-1",
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path,
        settings=make_settings(MODEL_STUDIO_WORKSPACE_ID="model-studio-secret"),
        client_factory=lambda _settings: FakeClient(),
        clock=lambda: 1000.0,
    )
    start_handle(tools)
    nested = {
        "token_bundle": {"value": "keep-me"},
        "items": [{"authorization_header": "Bearer real"}, ("api-key-secret", {"password_hint": "p@ss"})],
        "session_text": "sess-secret-123",
        "workspace": "model-studio-secret",
    }
    original = {
        "token_bundle": {"value": "keep-me"},
        "items": [{"authorization_header": "Bearer real"}, ("api-key-secret", {"password_hint": "p@ss"})],
        "session_text": "sess-secret-123",
        "workspace": "model-studio-secret",
    }

    redacted = tools._success(data=nested, artifact_references=[{"credential_blob": "abc"}])

    assert nested == original
    assert redacted["data"]["token_bundle"] == "[redacted]"
    assert redacted["data"]["items"][0]["authorization_header"] == "[redacted]"
    assert redacted["data"]["items"][1][0] == "[redacted]"
    assert redacted["data"]["items"][1][1]["password_hint"] == "[redacted]"
    assert redacted["data"]["session_text"] == "[redacted-session]"
    assert redacted["data"]["workspace"] == "[redacted]"
    assert redacted["artifact_references"][0]["credential_blob"] == "[redacted]"


def test_result_normalization_supports_casing_variants_and_error_aliases(tmp_path):
    tools, _, _ = make_tools(tmp_path)

    operation = tools._normalize_operation_result({"success": False, "requestId": "req-camel", "error": "broken"})
    static = AgentBayTools._normalize_static_result(
        types.SimpleNamespace(success=False, RequestId="req-pascal", ErrorMessage="still broken")
    )

    assert operation[:3] == (False, "req-camel", "broken")
    assert static[:3] == (False, "req-pascal", "still broken")


def test_workspace_staging_creates_parent_directories_preserves_exact_tree_and_hashes(tmp_path):
    source = tmp_path / "fixture"
    (source / "repo" / "services" / "api").mkdir(parents=True)
    (source / "public" / "assets").mkdir(parents=True)
    text_payload = "print('ok')\n"
    binary_payload = b"\xffPNG\x01"
    (source / "repo" / "services" / "api" / "auth.py").write_text(text_payload, encoding="utf-8")
    (source / "public" / "assets" / "hero.bin").write_bytes(binary_payload)
    events: list[dict[str, object]] = []
    client = FakeClient()
    tools = AgentBayTools(
        task_id="task-1",
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path / "artifacts",
        settings=make_settings(),
        client_factory=lambda _settings: client,
        clock=lambda: 1000.0,
        event_sink=events.append,
        workspace_source_dir=source,
    )

    start = tools.start_execution_environment("task-1", "stage")

    assert start["success"] is True
    file_system = client.session.file_system
    assert [item["path"] for item in file_system.create_directory_calls] == [
        "/workspace/public",
        "/workspace/public/assets",
        "/workspace/repo",
        "/workspace/repo/services",
        "/workspace/repo/services/api",
    ]
    assert file_system.write_calls[0]["path"] == "/workspace/repo/services/api/auth.py"
    assert file_system.upload_calls[0]["remote_path"] == "/workspace/public/assets/hero.bin"
    assert file_system.file_bytes["/workspace/public/assets/hero.bin"] == binary_payload
    staged = [event for event in events if event["event_type"] == "agentbay_workspace_stage_succeeded"][0]
    assert staged["source_tree_hash"] == staged["staged_tree_hash"]
    assert staged["verified_hashes"] == {
        "repo/services/api/auth.py": hashlib.sha256(file_system.file_bytes["/workspace/repo/services/api/auth.py"]).hexdigest()
    }


def test_workspace_staging_rejects_traversal_paths(tmp_path):
    tools, _, _ = make_tools(tmp_path)

    with pytest.raises(ValueError, match="traversal"):
        tools._normalize_workspace_relative_path("../escape.txt")


def test_workspace_staging_rejects_symlink_and_oversized_inputs(tmp_path, monkeypatch):
    source = tmp_path / "fixture"
    (source / "repo").mkdir(parents=True)
    target = source / "repo" / "auth.py"
    target.write_text("ok", encoding="utf-8")
    client = FakeClient()
    tools = AgentBayTools(
        task_id="task-1",
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path / "artifacts",
        settings=make_settings(),
        client_factory=lambda _settings: client,
        clock=lambda: 1000.0,
        workspace_source_dir=source,
    )
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path_obj):
        if path_obj == target:
            return True
        return original_is_symlink(path_obj)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    rejected = tools.start_execution_environment("task-1", "stage")
    assert rejected["success"] is False
    assert "symlink" in rejected["error_message"]

    monkeypatch.setattr(Path, "is_symlink", original_is_symlink)
    monkeypatch.setattr(agentbay_module, "MAX_STAGE_INPUT_FILE_BYTES", 1)
    oversized = tools.start_execution_environment("task-1", "stage")
    assert oversized["success"] is False
    assert "exceeds limit" in oversized["error_message"]


def test_workspace_staging_partial_failure_is_retained_for_cleanup(tmp_path):
    source = tmp_path / "fixture"
    (source / "repo").mkdir(parents=True)
    (source / "repo" / "one.txt").write_text("1", encoding="utf-8")
    (source / "repo" / "two.txt").write_text("2", encoding="utf-8")
    client = FakeClient()
    original_write = client.session.file_system.write_file
    call_count = {"count": 0}

    def flaky_write(path, content, mode):
        call_count["count"] += 1
        if call_count["count"] == 2:
            return FakeResult(success=False, request_id="req-write", error_message="boom")
        return original_write(path, content, mode)

    client.session.file_system.write_file = flaky_write
    tools = AgentBayTools(
        task_id="task-1",
        role_key="builder",
        allowed_remote_roots=["/workspace"],
        artifact_root=tmp_path / "artifacts",
        settings=make_settings(),
        client_factory=lambda _settings: client,
        clock=lambda: 1000.0,
        workspace_source_dir=source,
    )

    result = tools.start_execution_environment("task-1", "stage")

    assert result["success"] is False
    closed = tools.close_all()
    assert closed[0]["success"] is True
    assert client.delete_calls == ["sess-secret-123"]


@pytest.mark.parametrize(
    "result,pid",
    [
        (FakeResult(stdout="4312\n"), "4312"),
        (FakeResult(stdout=["4315\n"]), "4315"),
        (FakeResult(output='{"stdout":["4316\\n"]}'), "4316"),
        (FakeResult(logs=types.SimpleNamespace(stdout=["4317\n"])), "4317"),
        (FakeResult(output="server started; PID: 4313"), "4313"),
        (FakeResult(data={"processId": 4314}), "4314"),
    ],
)
def test_browser_server_bootstrap_accepts_agentbay_output_and_structured_pid_shapes(tmp_path, result, pid):
    tools, client, _events = make_tools(tmp_path, role_key="frontend_engineer")
    handle = start_handle(tools)
    client.session.command.next_result = result

    request_id, server_pid = tools._start_browser_server(
        tools._get_state(handle),
        agentbay_module.PurePosixPath("/workspace/app/dist/index.html"),
    )

    assert request_id == "req-1"
    assert server_pid == pid
    assert client.session.command.calls == [{
        "command": (
            "cd /workspace/app/dist && python3 -m http.server "
            f"{agentbay_module.BROWSER_RENDER_SERVER_PORT} --bind 127.0.0.1 "
            ">/tmp/qwendom-browser.log 2>&1 & echo $!"
        ),
        "timeout_ms": 20_000,
        "cwd": None,
        "envs": None,
    }]


def test_browser_server_bootstrap_rejects_success_response_without_explicit_pid(tmp_path):
    tools, client, _events = make_tools(tmp_path, role_key="frontend_engineer")
    handle = start_handle(tools)
    client.session.command.next_result = FakeResult(stdout="server started on localhost")

    with pytest.raises(RuntimeError, match="returned no PID"):
        tools._start_browser_server(
            tools._get_state(handle),
            agentbay_module.PurePosixPath("/workspace/app/dist/index.html"),
        )


def test_agentbay_browser_viewport_uses_sdk_model() -> None:
    """The SDK rejects a plain viewport dict before browser initialization."""

    _browser, BrowserOption, BrowserViewport = agentbay_module._load_agentbay_browser_types()
    option = BrowserOption(viewport=BrowserViewport(width=1440, height=900))

    assert option._to_map()["viewport"] == {"width": 1440, "height": 900}
