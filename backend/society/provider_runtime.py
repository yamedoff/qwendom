"""Provider-neutral reliability helpers for model API calls."""

from __future__ import annotations

import asyncio
import email.utils
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from .error_taxonomy import classify_error

T = TypeVar("T")

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_provider_error(value: object) -> str:
    """Return a log-safe provider error without credentials."""

    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", text)
    return text[:2000]


def retry_after_seconds(exc: BaseException) -> float | None:
    """Read Retry-After seconds or HTTP-date from common SDK exceptions."""

    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class ProviderCallError(RuntimeError):
    """Safe terminal provider failure suitable for events and API logs."""


async def run_provider_call(
    call: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_base_seconds: float = 1.0,
    backoff_cap_seconds: float = 30.0,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    """Run a model call with timeout, cancellation, and Retry-After support."""

    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(call(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = classify_error(exc)
            if category != "transient" or attempt >= attempts:
                raise ProviderCallError(redact_provider_error(exc)) from exc
            requested_delay = retry_after_seconds(exc)
            delay = requested_delay if requested_delay is not None else backoff_base_seconds * (2 ** (attempt - 1))
            delay = min(max(0.0, delay), max(0.0, backoff_cap_seconds))
            if on_retry is not None:
                on_retry({
                    "attempt": attempt,
                    "next_attempt": attempt + 1,
                    "delay_seconds": delay,
                    "reason": redact_provider_error(exc),
                    "retry_after_honored": requested_delay is not None,
                })
            await asyncio.sleep(delay)
    raise AssertionError("provider retry loop exited unexpectedly")
