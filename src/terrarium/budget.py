"""Concurrency-safe call gating and fail-closed token-usage accounting."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from .config import BudgetConfig


class BudgetExhausted(RuntimeError):
    """Raised when a call gate or reported-usage ceiling is exhausted."""


@dataclass(slots=True)
class BudgetUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failures: int = 0


class BudgetGovernor:
    """Reserves the hard call count and accounts reported tokens afterwards.

    Unknown token usage is treated as a failure.  The caller may pause a run,
    but the governor never changes the configured adapter or model silently.

    Token ceilings are an integrity/audit control, not a hard provider-spend cap:
    arbitrary subprocesses self-report usage and concurrent calls may already be in
    flight when a report crosses a ceiling.  A production cost boundary must meter
    at a host-owned provider broker and reserve known input plus worst-case output
    before dispatch.
    """

    def __init__(self, limits: BudgetConfig, usage: BudgetUsage | None = None) -> None:
        self.limits = limits
        self.usage = usage or BudgetUsage()
        self._lock = asyncio.Lock()

    async def reserve_call(self) -> None:
        async with self._lock:
            if self.usage.calls >= self.limits.max_calls:
                raise BudgetExhausted("model call budget exhausted")
            if self.usage.input_tokens >= self.limits.max_input_tokens:
                raise BudgetExhausted("input token accounting ceiling exhausted")
            if self.usage.output_tokens >= self.limits.max_output_tokens:
                raise BudgetExhausted("output token accounting ceiling exhausted")
            if self.usage.failures > self.limits.max_failures:
                raise BudgetExhausted("model failure budget exhausted")
            self.usage.calls += 1

    async def record(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        success: bool,
    ) -> None:
        async with self._lock:
            if input_tokens is None or output_tokens is None:
                self.usage.failures += 1
                raise BudgetExhausted("adapter omitted token usage; run paused fail-closed")
            if input_tokens < 0 or output_tokens < 0:
                self.usage.failures += 1
                raise BudgetExhausted("adapter reported invalid negative token usage")
            self.usage.input_tokens += input_tokens
            self.usage.output_tokens += output_tokens
            if not success:
                self.usage.failures += 1
            if self.usage.input_tokens > self.limits.max_input_tokens:
                raise BudgetExhausted("input token budget exceeded")
            if self.usage.output_tokens > self.limits.max_output_tokens:
                raise BudgetExhausted("output token budget exceeded")
            if self.usage.failures > self.limits.max_failures:
                raise BudgetExhausted("model failure budget exceeded")

    def snapshot(self) -> dict[str, int]:
        return asdict(self.usage)
