from __future__ import annotations

import hashlib
import random
import time
from typing import Any, Literal

ErrorCategory = Literal[
    "transient",
    "format",
    "semantic",
    "capability",
    "user_decision",
    "deterministic",
]

RETRYABLE_CATEGORIES: frozenset[str] = frozenset({"transient", "format", "semantic"})
NON_RETRYABLE_CATEGORIES: frozenset[str] = frozenset({"user_decision", "deterministic"})

_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "rate limit",
    "rate_limit",
    "429",
    "503",
    "502",
    "504",
    "connection reset",
    "connection refused",
    "connection error",
    "server overloaded",
    "temporary",
    "try again",
    "service unavailable",
    "deadline exceeded",
    "asyncio.timeout",
    "timeouterror",
)

_FORMAT_MARKERS: tuple[str, ...] = (
    "could not parse",
    "validation failed",
    "invalid json",
    "schema",
    "no tool result",
    "did not call the required",
    "structured output",
    "output_schema",
    "jsondecodeerror",
)

_CAPABILITY_MARKERS: tuple[str, ...] = (
    "tool not available",
    "capability",
    "unauthorized",
    "not permitted",
    "missing tool",
    "tool not found",
    "access denied",
)

_USER_DECISION_MARKERS: tuple[str, ...] = (
    "user decision",
    "ambiguous",
    "needs clarification",
    "user must",
    "requires user",
    "waiting for user",
    "tied vote",
    "no valid winner",
)

_DETERMINISTIC_MARKERS: tuple[str, ...] = (
    "assertion",
    "test failed",
    "repeated failure",
    "deterministic",
    "acceptance check failed",
)


def classify_error(exc: BaseException | str, context: str = "") -> ErrorCategory:
    text = f"{type(exc).__name__ if isinstance(exc, BaseException) else ''} {exc} {context}".lower()
    for marker in _USER_DECISION_MARKERS:
        if marker in text:
            return "user_decision"
    for marker in _TRANSIENT_MARKERS:
        if marker in text:
            return "transient"
    for marker in _FORMAT_MARKERS:
        if marker in text:
            return "format"
    for marker in _CAPABILITY_MARKERS:
        if marker in text:
            return "capability"
    for marker in _DETERMINISTIC_MARKERS:
        if marker in text:
            return "deterministic"
    if isinstance(exc, BaseException):
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return "transient"
    return "transient"


def is_retryable(category: ErrorCategory) -> bool:
    return category in RETRYABLE_CATEGORIES


def compute_backoff(attempt: int, base: float = 1.0, cap: float = 30.0, jitter: bool = True) -> float:
    exp = min(base * (2 ** max(0, attempt - 1)), cap)
    if jitter:
        exp = exp * (0.5 + random.random() * 0.5)
    return round(exp, 3)


def make_idempotency_key(task_id: str, subtask_id: str, agent_id: str, attempt: int) -> str:
    raw = f"{task_id}:{subtask_id}:{agent_id}:{attempt}"
    return f"idem-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def build_attempt_record(
    subtask_id: str,
    agent_id: str,
    attempt_number: int,
    max_attempts: int,
    idempotency_key: str,
    category: ErrorCategory | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    return {
        "subtask_id": subtask_id,
        "agent_id": agent_id,
        "attempt_number": attempt_number,
        "max_attempts": max_attempts,
        "idempotency_key": idempotency_key,
        "category": category,
        "model": model,
        "provider": provider,
        "started_at": time.time(),
        "finished_at": None,
        "duration_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "error_message": None,
        "next_action": None,
        "exhaustion_reason": None,
    }
