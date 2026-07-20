"""Atomic budget ledger shared by both Layer B benchmark modes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_text
from .models import BudgetCaps, BudgetSnapshot, ToolTraceRecord, canonical_json


class BudgetExceededError(RuntimeError):
    """Raised when one deterministic debit exceeds the frozen cap."""


class AtomicBudgetLedger:
    """Persist every debit and enforce caps under concurrent races."""

    def __init__(self, caps: BudgetCaps, journal_path: Path) -> None:
        self._caps = caps
        self._journal_path = journal_path
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._snapshot = BudgetSnapshot()
        self._journal_lines: list[str] = []

    @property
    def caps(self) -> BudgetCaps:
        """Return the frozen caps for this ledger."""

        return self._caps

    async def debit(self, category: str, amount: float, *, metadata: dict[str, Any] | None = None) -> None:
        """Apply one atomic debit and persist the journal immediately."""

        async with self._lock:
            self._apply_debit_or_persist_failure(category, amount, metadata=metadata)

    async def set_wall_time(self, seconds: float) -> None:
        """Persist the truthful wall time once the attempt finishes."""

        async with self._lock:
            self._snapshot.wall_time_seconds = max(self._snapshot.wall_time_seconds, round(seconds, 6))
            if self._snapshot.wall_time_seconds > self._caps.wall_time_seconds:
                raise BudgetExceededError(
                    f"wall_time_seconds exceeded: {self._snapshot.wall_time_seconds} > {self._caps.wall_time_seconds}"
                )
            self._append_event(
                {
                    "event": "final_wall_time",
                    "seconds": self._snapshot.wall_time_seconds,
                    "snapshot": self.snapshot().model_dump(mode="json"),
                }
            )

    async def finalize_wall_time(self, seconds: float) -> dict[str, Any]:
        """Persist final wall time without overriding the primary outcome."""

        async with self._lock:
            self._snapshot.wall_time_seconds = max(self._snapshot.wall_time_seconds, round(seconds, 6))
            over_by = round(max(0.0, self._snapshot.wall_time_seconds - self._caps.wall_time_seconds), 6)
            payload = {
                "event": "final_wall_time",
                "seconds": self._snapshot.wall_time_seconds,
                "cap_seconds": self._caps.wall_time_seconds,
                "over_cap": over_by > 0.0,
                "over_by_seconds": over_by,
                "snapshot": self.snapshot().model_dump(mode="json"),
            }
            self._append_event(payload)
            if over_by > 0.0:
                self._append_event(
                    {
                        "event": "budget_overage",
                        "category": "wall_time_seconds",
                        "actual": self._snapshot.wall_time_seconds,
                        "cap": self._caps.wall_time_seconds,
                        "over_by": over_by,
                        "snapshot": self.snapshot().model_dump(mode="json"),
                    }
                )
            return payload

    def snapshot(self) -> BudgetSnapshot:
        """Return a deep copy of the current usage snapshot."""

        return BudgetSnapshot.model_validate(self._snapshot.model_dump(mode="json"))

    @asynccontextmanager
    async def tool_call(
        self,
        tool_id: str,
        *,
        category: str,
        amount: float,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[ToolTraceRecord]:
        """Track one tool-like action with concurrency accounting."""

        started_at = time.perf_counter()
        async with self._lock:
            next_concurrency = self._snapshot.current_concurrency + 1
            if next_concurrency > self._caps.peak_concurrency:
                raise BudgetExceededError(
                    f"peak_concurrency exceeded: {next_concurrency} > {self._caps.peak_concurrency}"
                )
            self._snapshot.current_concurrency = next_concurrency
            self._snapshot.peak_concurrency = max(self._snapshot.peak_concurrency, next_concurrency)
            self._append_event(
                {
                    "event": "tool_call_started",
                    "tool_id": tool_id,
                    "current_concurrency": next_concurrency,
                    "snapshot": self.snapshot().model_dump(mode="json"),
                    "metadata": metadata or {},
                }
            )
        try:
            yield ToolTraceRecord(
                tool_id=tool_id,
                category=category,
                amount=amount,
                started_at=started_at,
                finished_at=started_at,
                metadata=dict(metadata or {}),
            )
        finally:
            finished_at = time.perf_counter()
            async with self._lock:
                try:
                    self._apply_debit_or_persist_failure(category, amount, metadata=metadata)
                finally:
                    self._snapshot.current_concurrency = max(0, self._snapshot.current_concurrency - 1)
                    self._append_event(
                        {
                            "event": "tool_call_finished",
                            "tool_id": tool_id,
                            "category": category,
                            "amount": amount,
                            "started_at": started_at,
                            "finished_at": finished_at,
                            "current_concurrency": self._snapshot.current_concurrency,
                            "snapshot": self.snapshot().model_dump(mode="json"),
                            "metadata": metadata or {},
                        }
                    )

    def _apply_debit_or_persist_failure(
        self,
        category: str,
        amount: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if amount < 0:
            raise ValueError("negative debits are not allowed")
        debit_metadata = dict(metadata or {})
        current = getattr(self._snapshot, category)
        next_value = round(current + amount, 6) if isinstance(current, float) else int(current + amount)
        cap = getattr(self._caps, category)
        if next_value > cap:
            self._append_failed_debit_event(
                category=category,
                amount=amount,
                current=current,
                attempted_value=next_value,
                cap=cap,
                overage=round(next_value - cap, 6),
                metadata=debit_metadata,
            )
            raise BudgetExceededError(f"{category} exceeded: {next_value} > {cap}")
        if category in {"qwen_input_tokens", "qwen_output_tokens"}:
            total_current = self._snapshot.qwen_total_tokens
            total_next = int(total_current + amount)
            total_cap = self._caps.qwen_total_tokens
            if total_next > total_cap:
                self._append_failed_debit_event(
                    category="qwen_total_tokens",
                    amount=amount,
                    current=total_current,
                    attempted_value=total_next,
                    cap=total_cap,
                    overage=total_next - total_cap,
                    metadata={
                        **debit_metadata,
                        "trigger_category": category,
                        "attempted_component_value": next_value,
                    },
                )
                raise BudgetExceededError(f"qwen_total_tokens exceeded: {total_next} > {total_cap}")
        setattr(self._snapshot, category, next_value)
        if category in {"qwen_input_tokens", "qwen_output_tokens"}:
            self._snapshot.qwen_total_tokens = int(self._snapshot.qwen_input_tokens + self._snapshot.qwen_output_tokens)
        elif category == "qwen_total_tokens":
            self._snapshot.qwen_total_tokens = int(next_value)
        self._append_event(
            {
                "event": "debit",
                "category": category,
                "amount": amount,
                "snapshot": self.snapshot().model_dump(mode="json"),
                "metadata": debit_metadata,
            }
        )

    def _append_failed_debit_event(
        self,
        *,
        category: str,
        amount: float,
        current: int | float,
        attempted_value: int | float,
        cap: int | float,
        overage: int | float,
        metadata: dict[str, Any],
    ) -> None:
        self._append_event(
            {
                "event": "debit_rejected",
                "category": category,
                "amount": amount,
                "current": current,
                "attempted_value": attempted_value,
                "cap": cap,
                "overage": overage,
                "snapshot": self.snapshot().model_dump(mode="json"),
                "metadata": metadata,
            }
        )
    def _append_event(self, payload: dict[str, Any]) -> None:
        self._journal_lines.append(canonical_json(payload))
        atomic_write_text(self._journal_path, "\n".join(self._journal_lines) + "\n")
