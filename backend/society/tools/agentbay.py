from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from agno.tools import Toolkit
from agentbay import AgentBay, Config, CreateSessionParams

try:
    from config import Settings
except ImportError:  # pragma: no cover - exercised in package-loaded test paths.
    from ...config import Settings
from ..capability_registry import canonical_tool_id, get_role_capabilities
from ..error_taxonomy import classify_error


MAX_PURPOSE_LENGTH = 200
MAX_COMMAND_TIMEOUT_SECONDS = 300
MAX_CODE_TIMEOUT_SECONDS = 300
MAX_CODE_LENGTH = 32_000
MAX_READ_LENGTH = 64_000
MAX_WRITE_LENGTH = 64_000
MAX_COMMAND_OUTPUT_LENGTH = 16_000
MAX_LISTING_ENTRIES = 200
MAX_LISTING_TEXT_LENGTH = 24_000
MAX_ARTIFACTS_PER_HANDLE = 20
MAX_ARTIFACT_SIZE_BYTES = 10 * 1024 * 1024
MAX_REMOTE_PATH_LENGTH = 512
DEFAULT_ORPHAN_LIST_LIMIT = 50
DEFAULT_ORPHAN_MAX_PAGES = 4
MAX_BROWSER_RENDER_CALLS_PER_HANDLE = 1
MAX_BROWSER_EVIDENCE_TEXT_BYTES = 256_000
MAX_BROWSER_CONSOLE_MESSAGES = 25
MAX_BROWSER_NETWORK_EVENTS = 50
MAX_BROWSER_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_STAGE_INPUT_FILE_COUNT = 256
MAX_STAGE_INPUT_FILE_BYTES = 8 * 1024 * 1024
MAX_STAGE_INPUT_TOTAL_BYTES = 32 * 1024 * 1024
BROWSER_RENDER_ENTRYPOINT = PurePosixPath("/workspace/app/dist/index.html")
BROWSER_RENDER_SERVER_PORT = 38451
BROWSER_RENDER_VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1440, 900),
    ("mobile", 390, 844),
)
QWENDOM_LABEL = "qwendom"
SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-:=+")
REDACTED_VALUE = "[redacted]"
REDACTED_SESSION_VALUE = "[redacted-session]"
_REDACTED_KEY_MARKERS = ("token", "secret", "password", "api_key", "authorization", "credential")
_PYTHON_BLOCKED_IMPORT_ROOTS = frozenset(
    {
        "builtins",
        "importlib",
        "inspect",
        "marshal",
        "os",
        "pickle",
        "sys",
        "subprocess",
        "socket",
        "pathlib",
        "shutil",
        "ctypes",
        "multiprocessing",
        "requests",
        "types",
        "urllib",
        "http",
        "ftplib",
        "paramiko",
    }
)
_PYTHON_BLOCKED_MEMBER_NAMES = frozenset(
    {
        "system",
        "popen",
        "fork",
        "exec",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "open",
        "eval",
        "compile",
        "__import__",
    }
)
_PYTHON_BLOCKED_NAME_LOADS = frozenset(
    {
        "builtins",
        "__builtins__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
    }
)
_PYTHON_BLOCKED_SUBSCRIPT_KEYS = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "spawn",
        "fork",
    }
)
_JAVASCRIPT_BLOCKED_MODULES = frozenset(
    {"child_process", "fs", "net", "http", "https", "tls", "dgram", "worker_threads"}
)
_JAVASCRIPT_BLOCKED_IDENTIFIERS = frozenset(
    {"globalThis", "global", "window", "self", "process", "require", "module", "constructor"}
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _get_attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _truncate_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else f"{text[:limit]}...[truncated]"


def _matches_redacted_key(key: Any) -> bool:
    return any(marker in str(key).lower() for marker in _REDACTED_KEY_MARKERS)


def _comment_stripped_javascript(code: str) -> str:
    """Remove JavaScript comments while keeping strings to preserve executable forms."""

    result: list[str] = []
    i = 0
    length = len(code)
    in_single = False
    in_double = False
    in_template = False
    escape = False
    while i < length:
        current = code[i]
        nxt = code[i + 1] if i + 1 < length else ""
        if in_single:
            result.append(current)
            if escape:
                escape = False
            elif current == "\\":
                escape = True
            elif current == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            result.append(current)
            if escape:
                escape = False
            elif current == "\\":
                escape = True
            elif current == '"':
                in_double = False
            i += 1
            continue
        if in_template:
            result.append(current)
            if escape:
                escape = False
            elif current == "\\":
                escape = True
            elif current == "`":
                in_template = False
            i += 1
            continue
        if current == "/" and nxt == "/":
            i += 2
            while i < length and code[i] not in "\r\n":
                i += 1
            continue
        if current == "/" and nxt == "*":
            i += 2
            while i + 1 < length and not (code[i] == "*" and code[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if current == "'":
            in_single = True
        elif current == '"':
            in_double = True
        elif current == "`":
            in_template = True
        result.append(current)
        i += 1
    return "".join(result)


@dataclass(frozen=True)
class CommandTemplate:
    """Validated server-side command template definition."""

    command_id: str
    argument_names: tuple[str, ...]
    validators: Mapping[str, Callable[[Any], str]]
    render: Callable[[dict[str, str]], Sequence[str]]

    def build_tokens(self, arguments: Mapping[str, Any]) -> list[str]:
        normalized = dict(arguments)
        unexpected = sorted(set(normalized) - set(self.argument_names))
        if unexpected:
            raise ValueError(f"Unknown command arguments: {', '.join(unexpected)}")
        missing = [name for name in self.argument_names if name not in normalized]
        if missing:
            raise ValueError(f"Missing command arguments: {', '.join(missing)}")
        validated = {name: self.validators[name](normalized[name]) for name in self.argument_names}
        tokens = [str(token) for token in self.render(validated)]
        if not tokens:
            raise ValueError("Command template rendered no tokens")
        for token in tokens:
            if not token or any(ch not in SAFE_TOKEN_CHARS for ch in token):
                raise ValueError("Command token contains unsupported characters")
        return tokens


@dataclass
class _HandleState:
    handle: str
    task_id: str
    role_key: str
    allowed_remote_roots: tuple[PurePosixPath, ...]
    session: Any
    session_id: str
    created_at: float
    artifact_count: int = 0
    closed: bool = False
    closing: bool = False
    delete_pending: bool = False
    canceled: bool = False
    last_request_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True)
class _WorkspaceStageFile:
    """One validated local file staged into a bounded remote workspace tree."""

    relative_path: str
    remote_path: PurePosixPath
    size_bytes: int
    sha256: str
    raw: bytes
    is_binary: bool


def _default_client_factory(settings: Settings) -> AgentBay:
    return AgentBay(
        api_key=settings.agentbay_api_key,
        cfg=Config(region_id=settings.agentbay_region_id, endpoint=settings.agentbay_endpoint),
    )


def _load_agentbay_browser_types() -> tuple[Any, Any, Any]:
    from agentbay import Browser, BrowserOption, BrowserViewport

    return Browser, BrowserOption, BrowserViewport


def _load_sync_playwright() -> Callable[[], Any]:
    from playwright.sync_api import sync_playwright

    return sync_playwright


class AgentBayTools(Toolkit):
    """Bounded AgentBay execution toolkit with opaque handles and cleanup guarantees."""

    def __init__(
        self,
        *,
        task_id: str,
        role_key: str,
        allowed_remote_roots: Sequence[str],
        artifact_root: str | Path,
        settings: Settings,
        client_factory: Callable[[Settings], Any] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] | None = None,
        command_templates: Mapping[str, CommandTemplate] | None = None,
        extra_allowed_tool_ids: Sequence[str] | None = None,
        workspace_source_dir: str | Path | None = None,
        workspace_overlay_exports: Mapping[str, str] | None = None,
        workspace_stage_remote_root: str = "/workspace",
        workspace_stage_verify_remote: bool = True,
    ) -> None:
        super().__init__(name="agentbay_tools", auto_register=False)
        self._task_id = task_id
        self._role_key = role_key
        self._allowed_remote_roots = tuple(self._normalize_root(root) for root in allowed_remote_roots)
        self._artifact_root = Path(artifact_root)
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._settings = settings
        self._client_factory = client_factory or _default_client_factory
        self._event_sink = event_sink
        self._clock = clock or __import__("time").time
        self._command_templates = dict(command_templates or self._build_default_templates())
        self._extra_allowed_tool_ids = frozenset(canonical_tool_id(tool_id) for tool_id in (extra_allowed_tool_ids or ()))
        self._workspace_source_dir = Path(workspace_source_dir).resolve() if workspace_source_dir is not None else None
        self._workspace_overlay_exports = dict(workspace_overlay_exports or {})
        self._workspace_stage_remote_root = self._normalize_root(workspace_stage_remote_root)
        self._workspace_stage_verify_remote = bool(workspace_stage_verify_remote)
        self._handles: dict[str, _HandleState] = {}
        self._lock = threading.RLock()
        self._client: Any | None = None
        self._tool_names = (
            "start_execution_environment",
            "execute_command",
            "run_code",
            "read_text_file",
            "write_text_file",
            "list_files",
            "browser_render",
            "export_artifact",
            "close_execution_environment",
        )
        for name in self._tool_names:
            self.register(getattr(self, name))

    @staticmethod
    def _build_default_templates() -> dict[str, CommandTemplate]:
        def simple_value(value: Any) -> str:
            text = str(value).strip()
            if not text:
                raise ValueError("Argument must not be empty")
            if any(ord(ch) < 32 for ch in text):
                raise ValueError("Control characters are not allowed")
            return text

        return {
            "pytest_target": CommandTemplate(
                command_id="pytest_target",
                argument_names=("target",),
                validators={"target": simple_value},
                render=lambda args: ("python3", "-m", "pytest", args["target"], "-q"),
            ),
            "python_compile": CommandTemplate(
                command_id="python_compile",
                argument_names=("target",),
                validators={"target": simple_value},
                render=lambda args: ("python3", "-m", "compileall", args["target"]),
            ),
        }

    def _role_capability(self) -> Any:
        return get_role_capabilities(self._role_key)

    def _require_tool_allowed(self, tool_id: str) -> None:
        role = self._role_capability()
        allowed = {canonical_tool_id(item) for item in getattr(role, "allowed_tools", [])}
        allowed.update(self._extra_allowed_tool_ids)
        if canonical_tool_id(tool_id) not in allowed:
            raise PermissionError(f"Role '{self._role_key}' lacks {tool_id}")

    def _require_toolkit_authorization(self, task_id: str) -> None:
        if task_id != self._task_id:
            raise PermissionError("Task mismatch for this toolkit instance")
        role = self._role_capability()
        if role is None or "sandbox_execution" not in role.capabilities:
            raise PermissionError(f"Role '{self._role_key}' lacks sandbox_execution")
        if not self._settings.agentbay_api_key:
            raise PermissionError("Missing AGENTBAY_API_KEY")

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self._settings)
        return self._client

    @staticmethod
    def _normalize_root(root: str) -> PurePosixPath:
        path = PurePosixPath(root)
        if not path.is_absolute():
            raise ValueError("Allowed remote roots must be absolute POSIX paths")
        return path

    def _normalize_remote_path(self, path: str, roots: Sequence[PurePosixPath] | None = None) -> PurePosixPath:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Path must be a non-empty string")
        if len(path) > MAX_REMOTE_PATH_LENGTH:
            raise ValueError("Path exceeds maximum length")
        if "\x00" in path or any(ord(ch) < 32 for ch in path):
            raise ValueError("Path contains control characters")
        normalized = PurePosixPath(path)
        if not normalized.is_absolute():
            raise ValueError("Path must be absolute")
        if ".." in normalized.parts:
            raise ValueError("Path traversal is not allowed")
        for root in roots or self._allowed_remote_roots:
            try:
                normalized.relative_to(root)
                return normalized
            except ValueError:
                continue
        raise ValueError("Path is outside allowed roots")

    def _safe_token(self, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("Argument must not be empty")
        if any(ord(ch) < 32 for ch in text):
            raise ValueError("Control characters are not allowed")
        if any(ch not in SAFE_TOKEN_CHARS for ch in text):
            raise ValueError("Shell metacharacters are not allowed")
        if ".." in text.split("/"):
            raise ValueError("Path traversal is not allowed")
        return text

    def _safe_remote_path_token(self, value: Any) -> str:
        return str(self._normalize_remote_path(str(value)))

    def _safe_language(self, language: str) -> str:
        normalized = language.strip().lower()
        if normalized not in {"python", "javascript"}:
            raise ValueError("Language must be python or javascript")
        return normalized

    def _bounded_timeout(self, timeout_seconds: int | float, upper: int) -> int:
        try:
            timeout = int(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("Timeout must be an integer number of seconds") from exc
        if timeout <= 0 or timeout > upper:
            raise ValueError(f"Timeout must be between 1 and {upper} seconds")
        return timeout

    def _new_handle(self) -> str:
        return secrets.token_urlsafe(24)

    def _sanitize_request_id(self, request_id: Any) -> str:
        return _truncate_text(request_id, 200)

    @staticmethod
    def _normalize_workspace_relative_path(path: str) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Workspace staging path must be a non-empty string")
        normalized = PurePosixPath(path.replace("\\", "/"))
        if normalized.is_absolute():
            raise ValueError("Workspace staging path must be relative")
        if ".." in normalized.parts:
            raise ValueError("Workspace staging path traversal is not allowed")
        if any(not part or part == "." for part in normalized.parts):
            raise ValueError("Workspace staging path contains an invalid segment")
        return normalized.as_posix()

    def _known_secret_values(self, *, extra_secrets: Sequence[str] | None = None) -> tuple[list[str], list[str]]:
        configured_values = [
            getattr(self._settings, "agentbay_api_key", ""),
            getattr(self._settings, "qwen_api_key", ""),
            getattr(self._settings, "dashscope_api_key", ""),
            getattr(self._settings, "model_studio_workspace_id", ""),
        ]
        if extra_secrets:
            configured_values.extend(str(item) for item in extra_secrets if item)
        with self._lock:
            session_ids = [state.session_id for state in self._handles.values() if state.session_id]
        return [str(value) for value in configured_values if value], session_ids

    def _redact_text(self, text: Any, *, extra_secrets: Sequence[str] | None = None) -> str:
        redacted = "" if text is None else str(text)
        secrets_to_mask, session_ids = self._known_secret_values(extra_secrets=extra_secrets)
        for secret in secrets_to_mask:
            redacted = redacted.replace(secret, REDACTED_VALUE)
        for session_id in session_ids:
            redacted = redacted.replace(session_id, REDACTED_SESSION_VALUE)
        return redacted

    def _redact_value(self, value: Any, *, key_hint: str | None = None, extra_secrets: Sequence[str] | None = None) -> Any:
        """Return a redacted copy without mutating caller-provided objects."""

        if _matches_redacted_key(key_hint):
            return REDACTED_VALUE
        if isinstance(value, Mapping):
            return {
                key: self._redact_value(item, key_hint=str(key), extra_secrets=extra_secrets)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item, key_hint=key_hint, extra_secrets=extra_secrets) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, key_hint=key_hint, extra_secrets=extra_secrets) for item in value)
        if isinstance(value, set):
            return {self._redact_value(item, key_hint=key_hint, extra_secrets=extra_secrets) for item in value}
        if isinstance(value, frozenset):
            return frozenset(self._redact_value(item, key_hint=key_hint, extra_secrets=extra_secrets) for item in value)
        if value is None:
            return None
        return self._redact_text(value, extra_secrets=extra_secrets) if isinstance(value, str) else value

    @staticmethod
    def _result_field(result: Any, payload: Mapping[str, Any], *names: str, default: Any = "") -> Any:
        for name in names:
            if isinstance(result, Mapping) and name in result:
                return result[name]
            if hasattr(result, name):
                return getattr(result, name)
            if name in payload:
                return payload[name]
        return default

    @staticmethod
    def _python_member_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return None

    @staticmethod
    def _python_attribute_chain(node: ast.AST) -> tuple[str, ...]:
        parts: list[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return tuple(reversed(parts))
        return ()

    @staticmethod
    def _python_root_name(node: ast.AST) -> str | None:
        current = node
        while True:
            if isinstance(current, ast.Name):
                return current.id
            if isinstance(current, ast.Attribute):
                current = current.value
                continue
            if isinstance(current, ast.Subscript):
                current = current.value
                continue
            if isinstance(current, ast.Call):
                current = current.func
                continue
            return None

    @staticmethod
    def _python_string_literal(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _validate_python_source(self, code: str) -> str | None:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return "Python source failed syntax validation"

        asyncio_aliases = {"asyncio"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in _PYTHON_BLOCKED_IMPORT_ROOTS:
                        return "Python source imports a restricted module"
                    if alias.name == "asyncio":
                        asyncio_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                root = module_name.split(".", 1)[0]
                if root in _PYTHON_BLOCKED_IMPORT_ROOTS:
                    return "Python source imports a restricted module"
                if module_name == "asyncio.subprocess" or (
                    module_name == "asyncio" and any(alias.name == "subprocess" for alias in node.names)
                ):
                    return "Python source references a restricted async subprocess API"
            elif isinstance(node, ast.Attribute):
                if node.attr.startswith("__") and node.attr.endswith("__"):
                    return "Python source uses a restricted dunder attribute"
                chain = self._python_attribute_chain(node)
                if len(chain) >= 2 and chain[0] in asyncio_aliases and chain[1] == "subprocess":
                    return "Python source references a restricted async subprocess API"
                if node.attr in _PYTHON_BLOCKED_MEMBER_NAMES:
                    return "Python source uses a restricted capability"
            elif isinstance(node, ast.Call):
                member_name = self._python_member_name(node.func)
                if member_name in _PYTHON_BLOCKED_MEMBER_NAMES or member_name in _PYTHON_BLOCKED_NAME_LOADS:
                    return "Python source uses a restricted capability"
            elif isinstance(node, ast.Subscript):
                root_name = self._python_root_name(node.value)
                if root_name in _PYTHON_BLOCKED_NAME_LOADS:
                    return "Python source uses a restricted capability"
                key_name = self._python_string_literal(node.slice)
                if key_name in _PYTHON_BLOCKED_SUBSCRIPT_KEYS:
                    return "Python source uses a restricted capability"
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id == "__import__" or node.id in _PYTHON_BLOCKED_NAME_LOADS:
                    return "Python source uses a restricted capability"
        return None

    @staticmethod
    def _javascript_property_bracket_expression(code: str, start_index: int) -> tuple[str, int] | None:
        previous_index = start_index - 1
        while previous_index >= 0 and code[previous_index].isspace():
            previous_index -= 1
        if previous_index < 0:
            return None
        previous_char = code[previous_index]
        if not (previous_char.isalnum() or previous_char in "_$)]"):
            return None

        depth = 1
        i = start_index + 1
        in_single = False
        in_double = False
        in_template = False
        escape = False
        while i < len(code):
            char = code[i]
            if in_single:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == "'":
                    in_single = False
                i += 1
                continue
            if in_double:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_double = False
                i += 1
                continue
            if in_template:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == "`":
                    in_template = False
                i += 1
                continue
            if char == "'":
                in_single = True
            elif char == '"':
                in_double = True
            elif char == "`":
                in_template = True
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return code[start_index + 1 : i], i
            i += 1
        return None

    @staticmethod
    def _javascript_contains_dynamic_property_expression(expression: str) -> bool:
        return any(marker in expression for marker in ("'", '"', "`", "+"))

    def _validate_javascript_source(self, code: str) -> str | None:
        stripped = _comment_stripped_javascript(code)
        pairs = {"(": ")", "[": "]", "{": "}"}
        closers = {value: key for key, value in pairs.items()}
        stack: list[str] = []
        in_single = False
        in_double = False
        in_template = False
        escape = False
        i = 0
        while i < len(stripped):
            char = stripped[i]
            if in_single:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == "'":
                    in_single = False
                i += 1
                continue
            if in_double:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_double = False
                i += 1
                continue
            if in_template:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == "`":
                    in_template = False
                i += 1
                continue
            if char == "'":
                in_single = True
            elif char == '"':
                in_double = True
            elif char == "`":
                in_template = True
            elif char.isalpha() or char in "_$":
                end = i + 1
                while end < len(stripped) and (stripped[end].isalnum() or stripped[end] in "_$"):
                    end += 1
                if stripped[i:end] in _JAVASCRIPT_BLOCKED_IDENTIFIERS:
                    return "JavaScript source uses a restricted capability"
                i = end - 1
            elif char in pairs:
                if char == "[":
                    bracket_expression = self._javascript_property_bracket_expression(stripped, i)
                    if bracket_expression is not None:
                        expression, end_index = bracket_expression
                        if self._javascript_contains_dynamic_property_expression(expression):
                            return "JavaScript source uses a restricted capability"
                        i = end_index
                    else:
                        stack.append(char)
                else:
                    stack.append(char)
            elif char in closers:
                if not stack or stack[-1] != closers[char]:
                    return "JavaScript source failed syntax validation"
                stack.pop()
            i += 1
        if in_single or in_double or in_template or stack:
            return "JavaScript source failed syntax validation"

        executable_patterns = [
            r"""\b(?:require|import)\s*\(\s*['"](?:fs|net|http|https|tls|dgram|worker_threads|child_process)['"]\s*\)""",
            r"""\bimport\s+(?:[^;]*?\s+from\s+)?['"](?:fs|net|http|https|tls|dgram|worker_threads|child_process)['"]""",
            r"""\b(?:import|export)\s+[^;]*?\bfrom\s+['"](?:fs|net|http|https|tls|dgram|worker_threads|child_process)['"]""",
            r"\bchild_process\b",
            r"\bDeno\b",
            r"\bBun\b",
            r"\bprocess\s*\.\s*binding\b",
            r"\bprocess\s*\.\s*mainModule\b",
            r"\b(?:eval|exec|spawn|fork)\s*\(",
            r"\bnew\s+Function\s*\(",
            r"\bFunction\s*\(",
            r"\b(?:fs|process)\s*\.\s*(?:open|readFile|writeFile|unlink|rm|rmdir|mkdir|readdir|rename|chmod|chown|symlink|link)\b",
        ]
        for pattern in executable_patterns:
            if re.search(pattern, stripped, flags=re.IGNORECASE | re.MULTILINE):
                return "JavaScript source uses a restricted capability"
        return None

    def _validate_source_policy(self, language: str, code: str) -> str | None:
        if language == "python":
            return self._validate_python_source(code)
        if language == "javascript":
            return self._validate_javascript_source(code)
        return "Unsupported language"

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._event_sink is None:
            return
        event = {"event_type": event_type, "task_id": self._task_id, "role_key": self._role_key}
        for key, value in payload.items():
            event[key] = self._redact_value(value, key_hint=key)
        self._event_sink(event)

    def _success(
        self,
        *,
        request_id: Any = "",
        output: str = "",
        data: Any = None,
        artifact_references: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "request_id": self._sanitize_request_id(request_id),
            "output": self._redact_text(_truncate_text(output, MAX_COMMAND_OUTPUT_LENGTH)),
            "data": self._redact_value(data),
            "artifact_references": self._redact_value(artifact_references or []),
            "error_category": None,
            "error_code": None,
            "error_message": None,
        }

    def _failure(self, error_code: str, message: str, *, request_id: Any = "", category: str | None = None) -> dict[str, Any]:
        resolved = category or classify_error(message)
        return {
            "success": False,
            "request_id": self._sanitize_request_id(request_id),
            "output": "",
            "data": None,
            "artifact_references": [],
            "error_category": resolved,
            "error_code": error_code,
            "error_message": self._redact_text(_truncate_text(message, MAX_COMMAND_OUTPUT_LENGTH)),
        }

    def _normalize_operation_result(self, result: Any) -> tuple[bool, str, str, dict[str, Any]]:
        if isinstance(result, bool):
            return result, "", "", {}
        payload = _as_mapping(result)
        success = bool(self._result_field(result, payload, "success", "Success", default=True))
        request_id = self._result_field(result, payload, "request_id", "requestId", "RequestId", default="") or ""
        error_message = (
            self._result_field(
                result,
                payload,
                "error_message",
                "errorMessage",
                "ErrorMessage",
                "ErrMsg",
                "error",
                default="",
            )
            or ""
        )
        extras = {}
        if hasattr(result, "__dict__"):
            extras.update({k: v for k, v in vars(result).items() if not k.startswith("_")})
        extras.update(payload)
        self._populate_public_collection_extras(result, payload, extras)
        return success, str(request_id), str(error_message), extras

    @staticmethod
    def _populate_public_collection_extras(result: Any, payload: Mapping[str, Any], extras: dict[str, Any]) -> None:
        """Preserve known public collection properties even when the SDK stores backing data privately."""

        for field_name in ("entries", "files", "items"):
            if field_name in extras or field_name in payload:
                continue
            value = AgentBayTools._result_field(result, payload, field_name, default=None)
            if value is not None:
                extras[field_name] = value

    def _session_id_from_session(self, session: Any) -> str:
        for name in ("session_id", "id", "SessionId"):
            value = _get_attr(session, name)
            if value:
                return str(value)
        info = getattr(session, "info", None)
        if callable(info):
            try:
                details = info()
            except Exception:
                details = None
            for name in ("session_id", "id", "SessionId"):
                value = _get_attr(details, name)
                if value:
                    return str(value)
        return ""

    def _get_state(self, handle: str, *, require_open: bool = True) -> _HandleState:
        with self._lock:
            state = self._handles.get(handle)
        if state is None or state.task_id != self._task_id:
            raise PermissionError("Unknown handle")
        if require_open:
            self._ensure_state_usable(state)
        return state

    @staticmethod
    def _ensure_state_usable(state: _HandleState) -> None:
        if state.canceled:
            raise TimeoutError("Execution environment has been cancelled")
        if state.closed:
            raise ValueError("Execution environment is already closed")
        if state.closing:
            raise ValueError("Execution environment is already closing")

    @contextmanager
    def _locked_handle_state(self, handle: str, *, require_open: bool = True) -> Any:
        state = self._get_state(handle, require_open=False)
        with state.operation_lock:
            if require_open:
                self._ensure_state_usable(state)
            yield state

    @staticmethod
    def _entry_name_and_path(entry: Any) -> tuple[str, str]:
        item = _as_mapping(entry)
        name = str(
            _get_attr(
                entry,
                "name",
                item.get("name", item.get("basename", item.get("filename", item.get("file_name", "")))),
            )
            or ""
        )
        path_value = str(
            _get_attr(
                entry,
                "path",
                item.get("path", item.get("full_path", item.get("fullPath", item.get("uri", name)))),
            )
            or ""
        )
        return name, path_value

    @staticmethod
    def _entry_size_bytes(entry: Any) -> int | None:
        item = _as_mapping(entry)
        value = _get_attr(
            entry,
            "size",
            item.get("size", item.get("size_bytes", item.get("sizeBytes", item.get("bytes")))),
        )
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value) if value >= 0 else None
        return None

    @staticmethod
    def _entry_type(entry: Any) -> str:
        item = _as_mapping(entry)
        raw_type = _get_attr(entry, "type", item.get("type", item.get("kind", item.get("entry_type", ""))))
        if raw_type:
            return str(raw_type)
        if bool(_get_attr(entry, "is_file", item.get("is_file", item.get("isFile", False)))):
            return "file"
        if bool(_get_attr(entry, "is_directory", item.get("is_directory", item.get("isDirectory", False)))):
            return "directory"
        return "unknown"

    def _find_remote_entry(self, entries: Sequence[Any], normalized: PurePosixPath) -> Any | None:
        target_name = normalized.name
        target_path = str(normalized)
        for entry in entries:
            entry_name, entry_path = self._entry_name_and_path(entry)
            if entry_path == target_path or entry_name == target_name:
                return entry
            if entry_path and PurePosixPath(entry_path).name == target_name:
                return entry
        return None

    def _artifact_dir_for_task(self) -> Path:
        digest = hashlib.sha256(self._task_id.encode("utf-8")).hexdigest()[:12]
        path = self._artifact_root / digest
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_local_artifact(self, *, artifact_kind: str, file_name: str, payload: bytes) -> dict[str, Any]:
        if len(payload) > MAX_BROWSER_SCREENSHOT_BYTES:
            raise ValueError("Artifact exceeds maximum size")
        task_dir = self._artifact_dir_for_task()
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in file_name)
        target = (task_dir / safe_name).resolve()
        if task_dir.resolve() not in target.parents:
            raise ValueError("Resolved artifact path escapes artifact root")
        target.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "id": f"artifact-{digest[:16]}",
            "type": artifact_kind,
            "producer": self._role_key,
            "path": str(target),
            "sha256": digest,
            "size_bytes": len(payload),
        }

    def _browser_entrypoint(self, path: str) -> PurePosixPath:
        normalized = self._normalize_remote_path(path)
        if normalized != BROWSER_RENDER_ENTRYPOINT:
            raise ValueError("Browser render only supports /workspace/app/dist/index.html")
        return normalized

    @staticmethod
    def _normalize_png_bytes(payload: Any) -> bytes:
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, bytearray):
            return bytes(payload)
        raise TypeError("Browser screenshot did not return PNG bytes")

    @staticmethod
    def _bounded_browser_json(value: Any) -> Any:
        text = json.dumps(value, sort_keys=True, ensure_ascii=True)
        if len(text.encode("utf-8")) <= MAX_BROWSER_EVIDENCE_TEXT_BYTES:
            return value
        return {"truncated": True, "preview": text[:MAX_BROWSER_EVIDENCE_TEXT_BYTES]}

    def _start_browser_server(self, state: _HandleState, entrypoint: PurePosixPath) -> tuple[str, str]:
        server_dir = entrypoint.parent
        # BrowserUse sessions expose the command service but not ``run_code``.
        # The entrypoint is already constrained to the fixed browser path, so
        # this runtime-owned command contains no model-controlled shell text.
        command = (
            f"cd {server_dir} && "
            f"python3 -m http.server {BROWSER_RENDER_SERVER_PORT} --bind 127.0.0.1 "
            ">/tmp/qwendom-browser.log 2>&1 & echo $!"
        )
        result = state.session.command.execute_command(command, 20_000)
        success, request_id, error_message, extras = self._normalize_operation_result(result)
        if not success:
            raise RuntimeError(error_message or "Browser server bootstrap failed")
        pid = self._extract_browser_server_pid(extras)
        if pid is None:
            raise RuntimeError("Browser server bootstrap returned no PID")
        state.last_request_id = request_id
        return request_id, pid

    @staticmethod
    def _extract_browser_server_pid(extras: Mapping[str, Any]) -> str | None:
        """Extract the bootstrap PID from documented AgentBay result shapes.

        AgentBay SDK versions expose code output either as stdout/output text or
        as a structured ``data``/``result`` mapping. Only explicit PID fields
        or a complete PID output line are accepted; arbitrary numbers are not.
        """

        def from_value(value: Any, *, allow_output_text: bool = False) -> str | None:
            if isinstance(value, bool) or value is None:
                return None
            if isinstance(value, int):
                return str(value) if value > 0 else None
            if isinstance(value, str):
                text = value.strip()
                if text.isdigit() and int(text) > 0:
                    return text
                if allow_output_text:
                    marked = re.search(r"(?im)\bpid\s*[:=]?\s*(\d+)\b", text)
                    if marked and int(marked.group(1)) > 0:
                        return marked.group(1)
                    line = re.search(r"(?m)^\s*(\d+)\s*$", text)
                    if line and int(line.group(1)) > 0:
                        return line.group(1)
                    if text.startswith(("{", "[")):
                        try:
                            decoded = json.loads(text)
                        except (TypeError, ValueError):
                            decoded = None
                        if isinstance(decoded, Mapping):
                            for nested_key in ("pid", "process_id", "processId", "stdout", "output", "result", "data"):
                                candidate = from_value(decoded.get(nested_key), allow_output_text=True)
                                if candidate is not None:
                                    return candidate
                        elif isinstance(decoded, list):
                            return from_value(decoded, allow_output_text=True)
                return None
            if isinstance(value, (list, tuple)):
                for item in value:
                    candidate = from_value(item, allow_output_text=allow_output_text)
                    if candidate is not None:
                        return candidate
            return None

        for key in ("pid", "process_id", "processId", "server_pid", "serverPid"):
            candidate = from_value(extras.get(key))
            if candidate is not None:
                return candidate
        for key in ("output", "stdout", "result", "data"):
            value = extras.get(key)
            candidate = from_value(value, allow_output_text=True)
            if candidate is not None:
                return candidate
            mapping = _as_mapping(value)
            for pid_key in ("pid", "process_id", "processId", "server_pid", "serverPid"):
                candidate = from_value(mapping.get(pid_key))
                if candidate is not None:
                    return candidate
        logs = extras.get("logs")
        candidate = from_value(_get_attr(logs, "stdout"), allow_output_text=True)
        if candidate is not None:
            return candidate
        return None

    def _stop_browser_server(self, state: _HandleState, server_pid: str) -> str:
        result = state.session.command.execute_command(f"kill {int(server_pid)}", 20_000)
        success, request_id, error_message, _ = self._normalize_operation_result(result)
        if not success:
            raise RuntimeError(error_message or "Browser server shutdown failed")
        state.last_request_id = request_id
        return request_id

    def _public_handle_data(self, state: _HandleState) -> dict[str, Any]:
        return {
            "handle": state.handle,
            "task_id": state.task_id,
            "role_key": state.role_key,
            "allowed_remote_roots": [str(root) for root in state.allowed_remote_roots],
        }

    def _collect_workspace_stage_inputs(
        self,
        source_root: Path,
        remote_root: PurePosixPath,
    ) -> tuple[list[PurePosixPath], list[_WorkspaceStageFile], dict[str, Any]]:
        source_root = source_root.resolve()
        if source_root.is_symlink():
            raise ValueError("Workspace staging source root cannot be a symlink")
        if not source_root.exists() or not source_root.is_dir():
            raise ValueError("Workspace staging source root must be an existing directory")

        directories: set[PurePosixPath] = set()
        files: list[_WorkspaceStageFile] = []
        total_bytes = 0

        for current_root, dir_names, file_names in os.walk(source_root, topdown=True, followlinks=False):
            current_path = Path(current_root)
            relative_dir = current_path.relative_to(source_root)
            if str(relative_dir) != ".":
                normalized_relative_dir = self._normalize_workspace_relative_path(relative_dir.as_posix())
                directories.add(self._normalize_remote_path(str(remote_root / normalized_relative_dir), [remote_root]))

            sanitized_dirs: list[str] = []
            for dir_name in sorted(dir_names):
                directory_path = current_path / dir_name
                if directory_path.is_symlink():
                    raise ValueError(f"Workspace staging rejects symlink directory: {directory_path}")
                sanitized_dirs.append(dir_name)
            dir_names[:] = sanitized_dirs

            for file_name in sorted(file_names):
                file_path = current_path / file_name
                if file_path.is_symlink():
                    raise ValueError(f"Workspace staging rejects symlink file: {file_path}")
                relative_path = self._normalize_workspace_relative_path(file_path.relative_to(source_root).as_posix())
                raw = file_path.read_bytes()
                size_bytes = len(raw)
                if size_bytes > MAX_STAGE_INPUT_FILE_BYTES:
                    raise ValueError(f"Workspace staging file exceeds limit: {relative_path}")
                total_bytes += size_bytes
                if total_bytes > MAX_STAGE_INPUT_TOTAL_BYTES:
                    raise ValueError("Workspace staging exceeds total byte limit")
                if len(files) + 1 > MAX_STAGE_INPUT_FILE_COUNT:
                    raise ValueError("Workspace staging exceeds file count limit")
                remote_path = self._normalize_remote_path(str(remote_root / relative_path), [remote_root])
                directories.add(remote_path.parent)
                try:
                    raw.decode("utf-8")
                    is_binary = False
                except UnicodeDecodeError:
                    is_binary = True
                files.append(
                    _WorkspaceStageFile(
                        relative_path=relative_path,
                        remote_path=remote_path,
                        size_bytes=size_bytes,
                        sha256=hashlib.sha256(raw).hexdigest(),
                        raw=raw,
                        is_binary=is_binary,
                    )
                )

        sorted_directories = sorted(
            (directory for directory in directories if directory != remote_root),
            key=str,
        )
        manifest = [
            {
                "path": item.relative_path,
                "remote_path": str(item.remote_path),
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "content_encoding": "binary" if item.is_binary else "utf-8",
            }
            for item in files
        ]
        return sorted_directories, files, {
            "source_root": str(source_root),
            "remote_root": str(remote_root),
            "file_count": len(files),
            "total_bytes": total_bytes,
            "manifest": manifest,
            "source_tree_hash": hashlib.sha256(
                json.dumps([(item["path"], item["sha256"]) for item in manifest], sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }

    def _verify_remote_stage_hashes(
        self,
        state: _HandleState,
        files: Sequence[_WorkspaceStageFile],
    ) -> dict[str, str]:
        verified: dict[str, str] = {}
        for item in files:
            if item.is_binary:
                continue
            result = state.session.file_system.read_file(str(item.remote_path), format="text")
            success, request_id, error_message, extras = self._normalize_operation_result(result)
            if not success:
                raise RuntimeError(error_message or f"Workspace staging verification failed for {item.relative_path}")
            state.last_request_id = request_id
            content = extras.get("content")
            if not isinstance(content, str):
                raise RuntimeError(f"Workspace staging verification returned no text content for {item.relative_path}")
            remote_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if remote_hash != item.sha256:
                raise RuntimeError(f"Workspace staging hash mismatch for {item.relative_path}")
            verified[item.relative_path] = remote_hash
        return verified

    def _stage_workspace_inputs(self, state: _HandleState) -> dict[str, Any] | None:
        source_root = self._workspace_source_dir
        if source_root is None:
            return None

        remote_root = self._workspace_stage_remote_root
        if remote_root not in state.allowed_remote_roots:
            raise ValueError("Workspace staging remote root is outside allowed remote roots")

        started_at = time.perf_counter()
        directories, files, summary = self._collect_workspace_stage_inputs(source_root, remote_root)
        request_ids: list[str] = []
        self._emit(
            "agentbay_workspace_stage_requested",
            handle=state.handle,
            source_root=summary["source_root"],
            remote_root=summary["remote_root"],
            source_tree_hash=summary["source_tree_hash"],
            file_count=summary["file_count"],
            total_bytes=summary["total_bytes"],
        )

        try:
            for directory in directories:
                result = state.session.file_system.create_directory(str(directory))
                success, request_id, error_message, _ = self._normalize_operation_result(result)
                request_ids.append(self._sanitize_request_id(request_id))
                state.last_request_id = request_id
                if not success:
                    raise RuntimeError(error_message or f"Failed to create directory {directory}")

            for item in files:
                if item.is_binary:
                    temp_file: str | None = None
                    try:
                        fd, temp_file = tempfile.mkstemp(prefix="agentbay-stage-", dir=str(self._artifact_root))
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(item.raw)
                            handle.flush()
                            os.fsync(handle.fileno())
                        result = state.session.file_system.upload_file(temp_file, str(item.remote_path))
                    finally:
                        if temp_file and os.path.exists(temp_file):
                            os.unlink(temp_file)
                else:
                    result = state.session.file_system.write_file(str(item.remote_path), item.raw.decode("utf-8"), "overwrite")
                success, request_id, error_message, _ = self._normalize_operation_result(result)
                request_ids.append(self._sanitize_request_id(request_id))
                state.last_request_id = request_id
                if not success:
                    raise RuntimeError(error_message or f"Failed to stage file {item.relative_path}")

            verified_hashes: dict[str, str] = {}
            if self._workspace_stage_verify_remote:
                verified_hashes = self._verify_remote_stage_hashes(state, files)

            finished_at = time.perf_counter()
            result = {
                "source_root": summary["source_root"],
                "remote_root": summary["remote_root"],
                "source_tree_hash": summary["source_tree_hash"],
                "staged_tree_hash": summary["source_tree_hash"],
                "file_count": summary["file_count"],
                "total_bytes": summary["total_bytes"],
                "manifest": [
                    {
                        **item,
                        "remote_verified": item["path"] in verified_hashes,
                    }
                    for item in summary["manifest"]
                ],
                "verified_hashes": verified_hashes,
                "request_ids": sorted(dict.fromkeys(request_ids)),
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": round(max(0.0, finished_at - started_at), 6),
            }
            state.metadata["workspace_staging"] = result
            self._emit("agentbay_workspace_stage_succeeded", handle=state.handle, **result)
            return result
        except Exception as exc:
            finished_at = time.perf_counter()
            self._emit(
                "agentbay_workspace_stage_failed",
                handle=state.handle,
                source_root=summary["source_root"],
                remote_root=summary["remote_root"],
                source_tree_hash=summary["source_tree_hash"],
                request_ids=sorted(dict.fromkeys(request_ids)),
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=round(max(0.0, finished_at - started_at), 6),
                error=str(exc),
            )
            raise

    def _stage_workspace_overlays(self, state: _HandleState) -> dict[str, Any] | None:
        """Overlay verified dependency exports onto the staged workspace for downstream validation."""

        if not self._workspace_overlay_exports:
            return None
        durable_root = self._artifact_root.resolve()
        remote_root = self._workspace_stage_remote_root
        manifest: list[dict[str, Any]] = []
        total_bytes = 0
        if len(self._workspace_overlay_exports) > MAX_STAGE_INPUT_FILE_COUNT:
            raise ValueError("Workspace overlay exceeds file-count limit")
        for raw_relative_path, raw_artifact_ref in sorted(self._workspace_overlay_exports.items()):
            relative_path = self._normalize_workspace_relative_path(raw_relative_path)
            artifact_ref = Path(raw_artifact_ref).resolve()
            try:
                artifact_ref.relative_to(durable_root)
            except ValueError as exc:
                raise ValueError("Workspace overlay artifact is outside the durable artifact root") from exc
            if not artifact_ref.is_file() or artifact_ref.is_symlink():
                raise ValueError(f"Workspace overlay artifact is not a durable regular file: {relative_path}")
            raw = artifact_ref.read_bytes()
            if len(raw) > MAX_STAGE_INPUT_FILE_BYTES:
                raise ValueError(f"Workspace overlay file exceeds limit: {relative_path}")
            total_bytes += len(raw)
            if total_bytes > MAX_STAGE_INPUT_TOTAL_BYTES:
                raise ValueError("Workspace overlay exceeds total size limit")
            remote_path = self._normalize_remote_path(str(remote_root / relative_path), [remote_root])
            parent_result = state.session.file_system.create_directory(str(remote_path.parent))
            parent_success, request_id, error_message, _ = self._normalize_operation_result(parent_result)
            state.last_request_id = request_id
            if not parent_success:
                raise RuntimeError(error_message or f"Failed to create overlay directory for {relative_path}")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                fd, temp_file = tempfile.mkstemp(prefix="agentbay-overlay-", dir=str(self._artifact_root))
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    write_result = state.session.file_system.upload_file(temp_file, str(remote_path))
                finally:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
            else:
                write_result = state.session.file_system.write_file(str(remote_path), content, "overwrite")
            success, request_id, error_message, _ = self._normalize_operation_result(write_result)
            state.last_request_id = request_id
            if not success:
                raise RuntimeError(error_message or f"Failed to stage workspace overlay {relative_path}")
            manifest.append({
                "path": relative_path,
                "remote_path": str(remote_path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            })
        result = {"file_count": len(manifest), "total_bytes": total_bytes, "manifest": manifest}
        state.metadata["workspace_overlays"] = result
        self._emit("agentbay_workspace_overlay_succeeded", handle=state.handle, **result)
        return result

    def start_execution_environment(self, task_id: str, purpose: str) -> dict[str, Any]:
        """Create an ephemeral AgentBay execution environment for the bound task."""

        try:
            self._require_toolkit_authorization(task_id)
            if not isinstance(purpose, str) or not purpose.strip():
                raise ValueError("Purpose must be a non-empty string")
            if len(purpose) > MAX_PURPOSE_LENGTH:
                raise ValueError("Purpose exceeds maximum length")
        except Exception as exc:
            self._emit("agentbay_start_rejected", error=str(exc))
            return self._failure("agentbay_start_rejected", str(exc))

        self._emit("agentbay_start_requested", purpose=purpose.strip())
        try:
            labels = {
                "managed_by": QWENDOM_LABEL,
                "task_id": self._task_id,
                "role_key": self._role_key,
                "created_at": str(int(self._clock())),
            }
            params = CreateSessionParams(
                labels=labels,
                image_id=(
                    self._settings.agentbay_browser_image_id
                    if self._role_key == "frontend_engineer"
                    else self._settings.agentbay_image_id
                ),
                idle_release_timeout=self._settings.agentbay_session_timeout_seconds,
            )
            result = self._get_client().create(params)
            success, request_id, error_message, extras = self._normalize_operation_result(result)
            if not success:
                return self._failure("agentbay_create_failed", error_message or "Session creation failed", request_id=request_id)
            session = _get_attr(result, "session", extras.get("session"))
            session_id = self._session_id_from_session(session)
            if session is None or not session_id:
                return self._failure("agentbay_create_invalid", "Session creation returned no usable session", request_id=request_id)
            state = _HandleState(
                handle=self._new_handle(),
                task_id=self._task_id,
                role_key=self._role_key,
                allowed_remote_roots=self._allowed_remote_roots,
                session=session,
                session_id=session_id,
                created_at=self._clock(),
                last_request_id=request_id,
            )
            with self._lock:
                self._handles[state.handle] = state
            if self._workspace_source_dir is not None:
                self._stage_workspace_inputs(state)
            if self._workspace_overlay_exports:
                self._stage_workspace_overlays(state)
            self._emit("agentbay_start_succeeded", handle=state.handle, request_id=request_id)
            return self._success(request_id=request_id, data=self._public_handle_data(state))
        except Exception as exc:
            self._emit("agentbay_start_failed", error=str(exc))
            return self._failure("agentbay_create_exception", str(exc))

    def execute_command(
        self,
        handle: str,
        command_id: str,
        arguments: Mapping[str, Any] | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Execute a validated allowlisted command in the remote session."""

        try:
            state = self._get_state(handle)
            timeout = self._bounded_timeout(timeout_seconds, MAX_COMMAND_TIMEOUT_SECONDS)
            template = self._command_templates.get(command_id)
            if template is None:
                raise ValueError("Unknown command_id")
            tokens = [self._safe_token(token) for token in template.build_tokens(arguments or {})]
        except Exception as exc:
            self._emit("agentbay_command_rejected", handle=handle, command_id=command_id, error=str(exc))
            return self._failure("agentbay_command_rejected", str(exc))

        self._emit("agentbay_command_requested", handle=handle, command_id=command_id, timeout_seconds=timeout)
        try:
            with self._locked_handle_state(handle) as locked_state:
                result = locked_state.session.command.execute_command(" ".join(tokens), timeout * 1000)
                success, request_id, error_message, extras = self._normalize_operation_result(result)
                if not success:
                    command_error = error_message or extras.get("stderr") or extras.get("output") or "Command execution failed"
                    self._emit(
                        "agentbay_command_failed",
                        handle=handle,
                        command_id=command_id,
                        request_id=request_id,
                        error=str(command_error),
                        exit_code=extras.get("exit_code", extras.get("code")),
                    )
                    return self._failure("agentbay_command_failed", str(command_error), request_id=request_id)
                output = "\n".join(text for text in (extras.get("stdout"), extras.get("stderr")) if text)
                data = {
                    "command_id": command_id,
                    "exit_code": extras.get("exit_code", extras.get("code", 0)),
                    "timed_out": bool(extras.get("timed_out", False)),
                }
                locked_state.last_request_id = request_id
            self._emit("agentbay_command_succeeded", handle=handle, command_id=command_id, request_id=request_id)
            return self._success(request_id=request_id, output=output, data=data)
        except Exception as exc:
            self._emit("agentbay_command_failed", handle=handle, command_id=command_id, error=str(exc))
            return self._failure("agentbay_command_exception", str(exc))

    def run_code(self, handle: str, language: str, code: str, timeout_seconds: int) -> dict[str, Any]:
        """Run bounded Python or JavaScript code without exposing session details."""

        try:
            state = self._get_state(handle)
            normalized_language = self._safe_language(language)
            timeout = self._bounded_timeout(timeout_seconds, MAX_CODE_TIMEOUT_SECONDS)
            if not isinstance(code, str) or not code.strip():
                raise ValueError("Code must be a non-empty string")
            if len(code) > MAX_CODE_LENGTH:
                raise ValueError("Code exceeds maximum length")
            policy_error = self._validate_source_policy(normalized_language, code)
            if policy_error:
                self._emit("agentbay_run_code_failed", handle=handle, language=normalized_language, error=policy_error)
                return self._failure("code_policy_violation", policy_error, category="capability")
        except Exception as exc:
            self._emit("agentbay_run_code_rejected", handle=handle, language=language, error=str(exc))
            return self._failure("agentbay_run_code_rejected", str(exc))

        self._emit("agentbay_run_code_requested", handle=handle, language=normalized_language, timeout_seconds=timeout)
        try:
            with self._locked_handle_state(handle) as locked_state:
                result = locked_state.session.code.run_code(code, normalized_language, timeout)
                success, request_id, error_message, extras = self._normalize_operation_result(result)
                if not success:
                    return self._failure("agentbay_run_code_failed", error_message or "Code execution failed", request_id=request_id)
                output = extras.get("output", extras.get("stdout", extras.get("result", "")))
                data = {"language": normalized_language, "timed_out": bool(extras.get("timed_out", False))}
                locked_state.last_request_id = request_id
            self._emit("agentbay_run_code_succeeded", handle=handle, language=normalized_language, request_id=request_id)
            return self._success(request_id=request_id, output=output, data=data)
        except Exception as exc:
            self._emit("agentbay_run_code_failed", handle=handle, language=language, error=str(exc))
            return self._failure("agentbay_run_code_exception", str(exc))

    def read_text_file(self, handle: str, path: str, offset: int = 0, length: int = MAX_READ_LENGTH) -> dict[str, Any]:
        """Read bounded text from an allowed remote file path."""

        try:
            state = self._get_state(handle)
            normalized = self._normalize_remote_path(path, state.allowed_remote_roots)
            start = max(int(offset), 0)
            size = int(length)
            if size <= 0 or size > MAX_READ_LENGTH:
                raise ValueError(f"Length must be between 1 and {MAX_READ_LENGTH}")
        except Exception as exc:
            return self._failure("agentbay_read_rejected", str(exc))

        try:
            with self._locked_handle_state(handle) as locked_state:
                result = locked_state.session.file_system.read_file(str(normalized), format="text")
                success, request_id, error_message, extras = self._normalize_operation_result(result)
                if not success:
                    return self._failure("agentbay_read_failed", error_message or "Read failed", request_id=request_id)
                text = str(extras.get("content", extras.get("data", extras.get("text", ""))))
                sliced = text[start : start + size]
                locked_state.last_request_id = request_id
            return self._success(
                request_id=request_id,
                data={"path": str(normalized), "offset": start, "length": len(sliced), "content": self._redact_text(sliced)},
            )
        except Exception as exc:
            return self._failure("agentbay_read_exception", str(exc))

    def write_text_file(self, handle: str, path: str, content: str, mode: str = "overwrite") -> dict[str, Any]:
        """Write bounded text to an allowed remote file path using overwrite or append."""

        try:
            state = self._get_state(handle)
            normalized = self._normalize_remote_path(path, state.allowed_remote_roots)
            selected_mode = mode.strip().lower()
            if selected_mode not in {"overwrite", "append"}:
                raise ValueError("Mode must be overwrite or append")
            if not isinstance(content, str):
                raise ValueError("Content must be text")
            if len(content) > MAX_WRITE_LENGTH:
                raise ValueError("Content exceeds maximum length")
        except Exception as exc:
            self._emit("agentbay_write_failed", path=path, mode=mode, error=str(exc))
            return self._failure("agentbay_write_rejected", str(exc))

        try:
            with self._locked_handle_state(handle) as locked_state:
                parent_result = locked_state.session.file_system.create_directory(str(normalized.parent))
                parent_success, parent_request_id, parent_error, _ = self._normalize_operation_result(parent_result)
                # Some AgentBay filesystem images report an existing directory as a
                # failed create. The write itself is the authoritative operation:
                # attempt it once, then fail closed if the parent was truly absent.
                result = locked_state.session.file_system.write_file(str(normalized), content, selected_mode)
                success, request_id, error_message, _ = self._normalize_operation_result(result)
                if not success:
                    combined_error = error_message or parent_error or "Write failed"
                    error_code = "agentbay_write_failed" if parent_success else "agentbay_write_parent_failed"
                    self._emit("agentbay_write_failed", path=str(normalized), mode=selected_mode, request_id=request_id or parent_request_id, error=combined_error)
                    return self._failure(error_code, combined_error, request_id=request_id or parent_request_id)
                if not parent_success:
                    self._emit(
                        "agentbay_parent_create_nonfatal",
                        path=str(normalized.parent),
                        request_id=parent_request_id,
                        reason=parent_error or "directory already existed",
                    )
                locked_state.last_request_id = request_id
            self._emit("agentbay_write_succeeded", path=str(normalized), mode=selected_mode, request_id=request_id)
            return self._success(request_id=request_id, data={"path": str(normalized), "mode": selected_mode})
        except Exception as exc:
            self._emit("agentbay_write_failed", path=str(normalized), mode=selected_mode, error=str(exc))
            return self._failure("agentbay_write_exception", str(exc))

    def list_files(self, handle: str, path: str) -> dict[str, Any]:
        """List a bounded set of file entries below an allowed remote path."""

        try:
            state = self._get_state(handle)
            normalized = self._normalize_remote_path(path, state.allowed_remote_roots)
        except Exception as exc:
            return self._failure("agentbay_list_rejected", str(exc))

        try:
            with self._locked_handle_state(handle) as locked_state:
                result = locked_state.session.file_system.list_directory(str(normalized))
                success, request_id, error_message, extras = self._normalize_operation_result(result)
                if not success:
                    return self._failure("agentbay_list_failed", error_message or "List failed", request_id=request_id)
                entries = extras.get("entries", extras.get("files", extras.get("items", [])))
                normalized_entries: list[dict[str, Any]] = []
                for entry in list(entries)[:MAX_LISTING_ENTRIES]:
                    entry_name, entry_path = self._entry_name_and_path(entry)
                    path_value = entry_path
                    if entry_name and (not path_value or not path_value.startswith("/")):
                        path_value = str(normalized / entry_name)
                    kind_value = self._entry_type(entry)
                    normalized_entries.append(
                        {
                            "path": self._redact_text(_truncate_text(path_value, 300)),
                            "type": self._redact_text(_truncate_text(kind_value, 40)),
                        }
                    )
                locked_state.last_request_id = request_id
            text_size = len(str(normalized_entries))
            if text_size > MAX_LISTING_TEXT_LENGTH:
                normalized_entries = normalized_entries[: max(1, MAX_LISTING_ENTRIES // 4)]
            return self._success(request_id=request_id, data={"path": str(normalized), "entries": normalized_entries})
        except Exception as exc:
            return self._failure("agentbay_list_exception", str(exc))

    def browser_render(self, handle: str, entry_html_path: str = "/workspace/app/dist/index.html") -> dict[str, Any]:
        """Render one bounded local frontend and capture desktop/mobile evidence artifacts."""

        operation_id = f"browser-render-{secrets.token_hex(6)}"
        browser_wrapper: Any | None = None
        playwright_manager: Any | None = None
        cdp_browser: Any | None = None
        page: Any | None = None
        server_pid: str | None = None
        state: _HandleState | None = None
        started_at = time.perf_counter()
        cleanup_steps: list[str] = []
        try:
            state = self._get_state(handle)
            self._require_tool_allowed("browser_render")
            entrypoint = self._browser_entrypoint(entry_html_path)
        except Exception as exc:
            self._emit("agentbay_browser_render_rejected", handle=handle, operation_id=operation_id, error=str(exc))
            return self._failure("agentbay_browser_render_rejected", str(exc))

        self._emit(
            "agentbay_browser_render_requested",
            handle=handle,
            operation_id=operation_id,
            entry_html_path=str(entrypoint),
            viewports=[{"name": name, "width": width, "height": height} for name, width, height in BROWSER_RENDER_VIEWPORTS],
        )
        try:
            with self._locked_handle_state(handle) as locked_state:
                render_calls = int(locked_state.metadata.get("browser_render_calls", 0))
                if render_calls >= MAX_BROWSER_RENDER_CALLS_PER_HANDLE:
                    raise ValueError("Browser render limit reached for this handle")
                locked_state.metadata["browser_render_calls"] = render_calls + 1
                state = locked_state

                browser_request_id, server_pid = self._start_browser_server(locked_state, entrypoint)
                cleanup_steps.append("loopback_server_started")
                Browser, BrowserOption, BrowserViewport = _load_agentbay_browser_types()
                sync_playwright = _load_sync_playwright()
                browser_wrapper = Browser(locked_state.session)
                initialized = browser_wrapper.initialize(
                    BrowserOption(viewport=BrowserViewport(width=1440, height=900))
                )
                if not initialized:
                    raise RuntimeError("AgentBay browser initialization failed")
                endpoint = str(browser_wrapper.get_endpoint_url() or "").strip()
                if not endpoint:
                    raise RuntimeError("AgentBay browser did not return a CDP endpoint")
                playwright_manager = sync_playwright().start()
                cdp_browser = playwright_manager.chromium.connect_over_cdp(endpoint)
                context = cdp_browser.contexts[0] if getattr(cdp_browser, "contexts", None) else cdp_browser.new_context()
                page = context.pages[0] if getattr(context, "pages", None) else context.new_page()

                artifact_references: list[dict[str, Any]] = []
                render_summaries: list[dict[str, Any]] = []
                for viewport_name, width, height in BROWSER_RENDER_VIEWPORTS:
                    console_messages: list[dict[str, Any]] = []
                    network_events: list[dict[str, Any]] = []

                    def on_console(message: Any) -> None:
                        if len(console_messages) >= MAX_BROWSER_CONSOLE_MESSAGES:
                            return
                        console_messages.append({
                            "type": str(getattr(message, "type", "")),
                            "text": self._redact_text(str(getattr(message, "text", "")))[:400],
                        })

                    def on_request_finished(request: Any) -> None:
                        if len(network_events) >= MAX_BROWSER_NETWORK_EVENTS:
                            return
                        response = request.response() if hasattr(request, "response") else None
                        network_events.append({
                            "status": getattr(response, "status", None),
                            "method": str(getattr(request, "method", "")),
                            "resource_type": str(getattr(request, "resource_type", "")),
                            "url": self._redact_text(str(getattr(request, "url", "")))[:400],
                        })

                    if hasattr(page, "on"):
                        page.on("console", on_console)
                        page.on("requestfinished", on_request_finished)
                    local_url = f"http://127.0.0.1:{BROWSER_RENDER_SERVER_PORT}/{entrypoint.name}"
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(local_url, wait_until="networkidle", timeout=15_000)
                    if hasattr(page, "wait_for_timeout"):
                        page.wait_for_timeout(200)
                    geometry = page.evaluate(
                        """() => {
                            const hero = document.querySelector('main') || document.body;
                            const heroBox = hero ? hero.getBoundingClientRect() : { width: 0, height: 0 };
                            const actionable = Array.from(document.querySelectorAll('button, a, input, select, textarea')).slice(0, 12);
                            return {
                                url: window.location.href,
                                title: document.title,
                                viewport: { width: window.innerWidth, height: window.innerHeight },
                                hero_width: Math.round(heroBox.width || 0),
                                hero_height: Math.round(heroBox.height || 0),
                                actionable_count: actionable.length,
                                actionable_labels: actionable.map((node) => (node.innerText || node.getAttribute('aria-label') || node.tagName || '').trim()).filter(Boolean).slice(0, 12),
                            };
                        }"""
                    )
                    interaction = page.evaluate(
                        """() => {
                            const button = document.querySelector('button, a');
                            if (!button) {
                                return { interacted: false, selector: null, text: null };
                            }
                            const text = (button.innerText || button.getAttribute('aria-label') || button.tagName || '').trim();
                            button.click();
                            return { interacted: true, selector: button.tagName.toLowerCase(), text };
                        }"""
                    )
                    accessibility = page.evaluate(
                        """() => ({
                            landmarks: Array.from(document.querySelectorAll('header, nav, main, aside, footer')).map((node) => node.tagName.toLowerCase()),
                            headings: Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6')).map((node) => ({ level: Number(node.tagName.slice(1)), text: (node.textContent || '').trim() })).slice(0, 40),
                            unlabeled_controls: Array.from(document.querySelectorAll('button, input, select, textarea')).filter((node) => !(node.getAttribute('aria-label') || node.getAttribute('aria-labelledby') || node.textContent || node.getAttribute('placeholder'))).length,
                            images_missing_alt: Array.from(document.images).filter((image) => !image.hasAttribute('alt')).length,
                        })"""
                    )
                    png_bytes = self._normalize_png_bytes(browser_wrapper.screenshot(page, full_page=True, type="png"))
                    screenshot_ref = self._write_local_artifact(
                        artifact_kind="browser_screenshot",
                        file_name=f"{operation_id}_{viewport_name}.png",
                        payload=png_bytes,
                    )
                    evidence_payload = {
                        "operation_id": operation_id,
                        "viewport_name": viewport_name,
                        "viewport": {"width": width, "height": height},
                        "url_kind": "session_loopback",
                        "geometry": geometry,
                        "interaction": interaction,
                        "console_messages": console_messages[:MAX_BROWSER_CONSOLE_MESSAGES],
                        "network_events": network_events[:MAX_BROWSER_NETWORK_EVENTS],
                        "accessibility_snapshot": self._bounded_browser_json(accessibility),
                        "screenshot_artifact_id": screenshot_ref["id"],
                    }
                    evidence_ref = self._write_local_artifact(
                        artifact_kind="browser_evidence",
                        file_name=f"{operation_id}_{viewport_name}.json",
                        payload=json.dumps(evidence_payload, indent=2, sort_keys=True).encode("utf-8"),
                    )
                    artifact_references.extend([screenshot_ref, evidence_ref])
                    render_summaries.append({
                        "viewport_name": viewport_name,
                        "width": width,
                        "height": height,
                        "screenshot_artifact": screenshot_ref,
                        "evidence_artifact": evidence_ref,
                        "geometry": geometry,
                        "interaction": interaction,
                        "console_message_count": len(console_messages),
                        "network_event_count": len(network_events),
                    })

                manifest_payload = {
                    "operation_id": operation_id,
                    "entry_html_path": str(entrypoint),
                    "browser_render_credits": len(BROWSER_RENDER_VIEWPORTS),
                    "viewports": render_summaries,
                    "workspace_export_hints": [
                        {"workspace_relative_path": "screenshots/desktop.png", "viewport_name": "desktop"},
                        {"workspace_relative_path": "screenshots/mobile.png", "viewport_name": "mobile"},
                    ],
                }
                manifest_ref = self._write_local_artifact(
                    artifact_kind="browser_manifest",
                    file_name=f"{operation_id}_manifest.json",
                    payload=json.dumps(manifest_payload, indent=2, sort_keys=True).encode("utf-8"),
                )
                artifact_references.append(manifest_ref)
                cleanup_steps.append("browser_render_completed")
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                self._emit(
                    "agentbay_browser_render_succeeded",
                    handle=handle,
                    operation_id=operation_id,
                    request_id=browser_request_id,
                    browser_render_credits=len(BROWSER_RENDER_VIEWPORTS),
                    elapsed_ms=elapsed_ms,
                    viewports=render_summaries,
                    manifest_artifact=manifest_ref,
                )
                return self._success(
                    request_id=browser_request_id,
                    data={
                        "operation_id": operation_id,
                        "browser_render_credits": len(BROWSER_RENDER_VIEWPORTS),
                        "elapsed_ms": elapsed_ms,
                        "entry_html_path": str(entrypoint),
                        "viewports": render_summaries,
                        "manifest_artifact": manifest_ref,
                        "workspace_export_hints": manifest_payload["workspace_export_hints"],
                    },
                    artifact_references=artifact_references,
                )
        except BaseException as exc:
            self._emit(
                "agentbay_browser_render_failed",
                handle=handle,
                operation_id=operation_id,
                error=str(exc),
                cleanup_steps=cleanup_steps,
            )
            if isinstance(exc, Exception):
                return self._failure("agentbay_browser_render_exception", str(exc))
            raise
        finally:
            cleanup_errors: list[str] = []
            if page is not None and hasattr(page, "close"):
                try:
                    page.close()
                    cleanup_steps.append("page_closed")
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if cdp_browser is not None and hasattr(cdp_browser, "close"):
                try:
                    cdp_browser.close()
                    cleanup_steps.append("cdp_closed")
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if playwright_manager is not None and hasattr(playwright_manager, "stop"):
                try:
                    playwright_manager.stop()
                    cleanup_steps.append("playwright_stopped")
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if browser_wrapper is not None and hasattr(browser_wrapper, "close"):
                try:
                    browser_wrapper.close()
                    cleanup_steps.append("browser_wrapper_closed")
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if server_pid is not None and state is not None:
                try:
                    self._stop_browser_server(state, server_pid)
                    cleanup_steps.append("loopback_server_stopped")
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            self._emit(
                "agentbay_browser_render_cleanup_completed",
                handle=handle,
                operation_id=operation_id,
                cleanup_steps=cleanup_steps,
                cleanup_errors=cleanup_errors,
            )

    def export_artifact(self, handle: str, path: str, artifact_kind: str) -> dict[str, Any]:
        """Download an allowed remote artifact into the local durable artifact store."""

        state: _HandleState | None = None
        temp_path: Path | None = None
        final_path: Path | None = None
        reservation_made = False
        size_preflighted = False
        try:
            state = self._get_state(handle)
            normalized = self._normalize_remote_path(path, state.allowed_remote_roots)
            if not isinstance(artifact_kind, str) or not artifact_kind.strip():
                raise ValueError("artifact_kind must be a non-empty string")
            safe_kind = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in artifact_kind.strip().lower())
            base_name = normalized.name or "artifact"
            task_dir = self._artifact_dir_for_task()
            unique = secrets.token_hex(8)
            final_path = (task_dir / f"{safe_kind}_{unique}_{base_name}").resolve()
            if task_dir.resolve() not in final_path.parents:
                raise ValueError("Resolved artifact path escapes artifact root")
            temp_path = final_path.with_suffix(final_path.suffix + ".part")
            self._emit("agentbay_artifact_export_requested", path=str(normalized), artifact_kind=safe_kind)
            with self._locked_handle_state(handle) as locked_state:
                if locked_state.artifact_count >= MAX_ARTIFACTS_PER_HANDLE:
                    raise ValueError("Artifact export limit reached for this handle")
                locked_state.artifact_count += 1
                reservation_made = True
                parent_listing = locked_state.session.file_system.list_directory(str(normalized.parent))
                list_success, request_id, list_error, list_extras = self._normalize_operation_result(parent_listing)
                if not list_success:
                    raise RuntimeError(list_error or "Artifact preflight failed")
                entries = list_extras.get("entries", list_extras.get("files", list_extras.get("items", [])))
                entry = self._find_remote_entry(list(entries), normalized)
                if entry is None:
                    raise FileNotFoundError("Artifact path was not found during preflight")
                known_size = self._entry_size_bytes(entry)
                size_preflighted = known_size is not None
                if known_size is not None and known_size > MAX_ARTIFACT_SIZE_BYTES:
                    raise ValueError("Artifact exceeds maximum size")
                result = locked_state.session.file_system.download_file(str(normalized), str(temp_path))
                success, request_id, error_message, _ = self._normalize_operation_result(result)
                if not success:
                    raise RuntimeError(error_message or "Artifact download failed")
                locked_state.last_request_id = request_id
            size = temp_path.stat().st_size
            if size > MAX_ARTIFACT_SIZE_BYTES:
                raise ValueError("Artifact exceeds maximum size")
            digest = hashlib.sha256(temp_path.read_bytes()).hexdigest()
            os.replace(temp_path, final_path)
            artifact_ref = {
                "id": f"artifact-{digest[:16]}",
                "type": safe_kind,
                "producer": self._role_key,
                "path": str(final_path),
                "sha256": digest,
                "size_bytes": size,
            }
            try:
                workspace_relative_path = normalized.relative_to(PurePosixPath("/workspace")).as_posix()
            except ValueError:
                workspace_relative_path = normalized.name
            self._emit(
                "agentbay_artifact_exported",
                request_id=request_id,
                workspace_relative_path=workspace_relative_path,
                artifact_ref={
                    "id": artifact_ref["id"],
                    "type": artifact_ref["type"],
                    "path": artifact_ref["path"],
                    "sha256": artifact_ref["sha256"],
                    "size_bytes": artifact_ref["size_bytes"],
                },
            )
            return self._success(
                request_id=request_id,
                artifact_references=[artifact_ref],
                data={"artifact": artifact_ref, "size_preflighted": size_preflighted},
            )
        except Exception as exc:
            if reservation_made and state is not None:
                with state.operation_lock:
                    state.artifact_count = max(0, state.artifact_count - 1)
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            if final_path is not None and final_path.exists() and final_path.stat().st_size == 0:
                final_path.unlink(missing_ok=True)
            self._emit(
                "agentbay_artifact_export_failed",
                path=path,
                artifact_kind=artifact_kind,
                size_preflighted=size_preflighted,
                error=str(exc),
            )
            failure = self._failure("agentbay_export_exception", str(exc))
            failure["data"] = self._redact_value({"size_preflighted": size_preflighted})
            return failure

    def _delete_state(self, state: _HandleState) -> tuple[bool, str, str]:
        state.closing = True
        try:
            result = self._get_client().delete(state.session)
            success, request_id, error_message, _ = self._normalize_operation_result(result)
            if success:
                state.closed = True
                state.delete_pending = False
            else:
                state.delete_pending = True
            state.last_request_id = str(request_id or state.last_request_id)
            return success, request_id, error_message
        finally:
            state.closing = False

    def close_execution_environment(self, handle: str) -> dict[str, Any]:
        """Delete a remote session. Repeated successful closes are idempotent."""

        try:
            state = self._get_state(handle, require_open=False)
        except Exception as exc:
            return self._failure("agentbay_close_rejected", str(exc))
        try:
            with state.operation_lock:
                if state.closed and not state.delete_pending:
                    return self._success(
                        request_id=state.last_request_id,
                        data={"handle": handle, "closed": True, "idempotent": True},
                    )
                success, request_id, error_message = self._delete_state(state)
                if success:
                    return self._success(request_id=request_id, data={"handle": handle, "closed": True, "idempotent": False})
                return self._failure("agentbay_close_failed", error_message or "Delete failed", request_id=request_id)
        except Exception as exc:
            with state.operation_lock:
                state.delete_pending = True
            return self._failure("agentbay_close_exception", str(exc))

    def cancel_and_close(self, handle: str) -> dict[str, Any]:
        """Mark a handle as cancelled and then best-effort close it outside tool registration."""

        try:
            state = self._get_state(handle, require_open=False)
        except Exception as exc:
            return self._failure("agentbay_cancel_rejected", str(exc))
        try:
            with state.operation_lock:
                if state.canceled:
                    canceled_request_id = state.last_request_id
                else:
                    state.canceled = True
                    canceled_request_id = state.last_request_id
                if state.closed and not state.delete_pending:
                    return self._success(
                        request_id=canceled_request_id,
                        data={"handle": handle, "closed": True, "canceled": True, "idempotent": True},
                    )
                success, request_id, error_message = self._delete_state(state)
                if success:
                    return self._success(
                        request_id=request_id,
                        data={"handle": handle, "closed": True, "canceled": True, "idempotent": False},
                    )
                return self._failure("agentbay_close_failed", error_message or "Delete failed", request_id=request_id)
        except Exception as exc:
            with state.operation_lock:
                state.delete_pending = True
                state.canceled = True
            return self._failure("agentbay_close_exception", str(exc))

    def close_all(self) -> list[dict[str, Any]]:
        """Attempt to close all known handles without aborting on individual failures."""

        with self._lock:
            handles = list(self._handles)
        results: list[dict[str, Any]] = []
        for handle in handles:
            state = self._get_state(handle, require_open=False)
            if state.canceled:
                results.append(self.cancel_and_close(handle))
            else:
                results.append(self.close_execution_environment(handle))
        return results

    @contextmanager
    def managed_execution_environment(self, task_id: str, purpose: str) -> Any:
        """Context manager that always attempts remote deletion on exit."""

        start_result = self.start_execution_environment(task_id, purpose)
        if not start_result["success"]:
            raise RuntimeError(start_result["error_message"])
        handle = str(start_result["data"]["handle"])
        try:
            yield handle
        finally:
            try:
                self.close_execution_environment(handle)
            except BaseException:
                pass

    @classmethod
    def reconcile_orphaned_sessions(
        cls,
        *,
        settings: Settings,
        active_task_ids: set[str],
        artifact_root: str | Path,
        client_factory: Callable[[Settings], Any] | None = None,
        now: float | None = None,
        max_pages: int = DEFAULT_ORPHAN_MAX_PAGES,
        page_limit: int = DEFAULT_ORPHAN_LIST_LIMIT,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Delete only expired or task-orphaned Qwendom-managed sessions."""

        if not settings.agentbay_api_key:
            return []
        clock_now = float(now if now is not None else __import__("time").time())
        factory = client_factory or _default_client_factory
        client = factory(settings)
        results: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            listing = client.list(labels={"managed_by": QWENDOM_LABEL}, page=page, limit=page_limit)
            success, request_id, error_message, extras = cls._normalize_static_result(listing)
            if not success:
                results.append(
                    {
                        "success": False,
                        "request_id": request_id,
                        "output": "",
                        "data": {"page": page},
                        "artifact_references": [],
                        "error_category": classify_error(error_message or "List failed"),
                        "error_code": "agentbay_reconcile_list_failed",
                        "error_message": _truncate_text(error_message or "List failed", MAX_COMMAND_OUTPUT_LENGTH),
                    }
                )
                break
            sessions = extras.get("sessions", extras.get("items", extras.get("data", [])))
            if not sessions:
                break
            for entry in list(sessions)[:page_limit]:
                item = _as_mapping(entry)
                labels = _get_attr(entry, "labels", item.get("labels", {})) or {}
                if labels.get("managed_by") != QWENDOM_LABEL:
                    continue
                task_id = str(labels.get("task_id", ""))
                created_at = str(labels.get("created_at", "0"))
                session_id = str(_get_attr(entry, "session_id", item.get("session_id", item.get("id", ""))))
                expired = False
                try:
                    expired = int(created_at or "0") + settings.agentbay_session_timeout_seconds <= int(clock_now)
                except ValueError:
                    expired = False
                orphaned = bool(task_id) and task_id not in active_task_ids
                if not expired and not orphaned:
                    continue
                get_result = client.get(session_id)
                get_success, _, get_error, get_extras = cls._normalize_static_result(get_result)
                if not get_success:
                    results.append(
                        {
                            "success": False,
                            "request_id": request_id,
                            "output": "",
                            "data": {"task_id": task_id},
                            "artifact_references": [],
                            "error_category": classify_error(get_error or "Get failed"),
                            "error_code": "agentbay_reconcile_get_failed",
                            "error_message": _truncate_text(get_error or "Get failed", MAX_COMMAND_OUTPUT_LENGTH),
                        }
                    )
                    continue
                session = _get_attr(get_result, "session", get_extras.get("session"))
                delete_result = client.delete(session)
                delete_success, delete_request_id, delete_error, _ = cls._normalize_static_result(delete_result)
                if event_sink is not None:
                    event_sink(
                        {
                            "event_type": "agentbay_reconcile_attempted",
                            "task_id": task_id,
                            "role_key": labels.get("role_key", ""),
                            "request_id": _truncate_text(delete_request_id or request_id, 200),
                        }
                    )
                results.append(
                    {
                        "success": delete_success,
                        "request_id": _truncate_text(delete_request_id or request_id, 200),
                        "output": "",
                        "data": {"task_id": task_id, "expired": expired, "orphaned": orphaned},
                        "artifact_references": [],
                        "error_category": None if delete_success else classify_error(delete_error or "Delete failed"),
                        "error_code": None if delete_success else "agentbay_reconcile_delete_failed",
                        "error_message": None if delete_success else _truncate_text(delete_error or "Delete failed", MAX_COMMAND_OUTPUT_LENGTH),
                    }
                )
            if len(list(sessions)) < page_limit:
                break
        return results

    @staticmethod
    def _normalize_static_result(result: Any) -> tuple[bool, str, str, dict[str, Any]]:
        if isinstance(result, bool):
            return result, "", "", {}
        payload = _as_mapping(result)
        success = bool(AgentBayTools._result_field(result, payload, "success", "Success", default=True))
        request_id = str(
            AgentBayTools._result_field(result, payload, "request_id", "requestId", "RequestId", default="") or ""
        )
        error_message = str(
            AgentBayTools._result_field(
                result,
                payload,
                "error_message",
                "errorMessage",
                "ErrorMessage",
                "ErrMsg",
                "error",
                default="",
            )
            or ""
        )
        extras = {}
        if hasattr(result, "__dict__"):
            extras.update({k: v for k, v in vars(result).items() if not k.startswith("_")})
        extras.update(payload)
        AgentBayTools._populate_public_collection_extras(result, payload, extras)
        return success, request_id, error_message, extras
