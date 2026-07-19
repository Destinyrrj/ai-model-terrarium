"""Ephemeral wall-clock observations for live GUI rates.

Durable Terrarium events intentionally have no wall-clock timestamps.  These
measurements live only in GUI process memory and are never written into a run
directory, so they cannot affect replay or audit hashes.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TickObservation:
    monotonic_seconds: float
    tick: int


class TickRateTracker:
    """Estimate committed ticks per minute over a bounded observation window."""

    def __init__(self, *, window_seconds: float = 60.0, max_samples: int = 512) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if type(max_samples) is not int or max_samples < 2:
            raise ValueError("max_samples must be at least two")
        self.window_seconds = float(window_seconds)
        self.max_samples = max_samples
        self._runs: dict[str, deque[TickObservation]] = {}

    def observe(
        self,
        run_name: str,
        tick: int,
        *,
        now: float | None = None,
    ) -> dict[str, int | float | None]:
        if not isinstance(run_name, str) or not run_name:
            raise ValueError("run_name must be a non-empty string")
        if type(tick) is not int or tick < 0:
            raise ValueError("tick must be a non-negative integer")
        instant = time.monotonic() if now is None else float(now)
        samples = self._runs.setdefault(run_name, deque(maxlen=self.max_samples))
        if samples and (tick < samples[-1].tick or instant < samples[-1].monotonic_seconds):
            samples.clear()
        if not samples or tick != samples[-1].tick:
            samples.append(TickObservation(instant, tick))
        self._trim(samples, instant)
        return self.snapshot(run_name)

    def snapshot(self, run_name: str) -> dict[str, int | float | None]:
        samples = self._runs.get(run_name)
        if not samples:
            return {
                "tick": None,
                "ticks_per_minute": None,
                "observed_ticks": 0,
                "observed_seconds": 0.0,
            }
        first = samples[0]
        last = samples[-1]
        elapsed = last.monotonic_seconds - first.monotonic_seconds
        delta = last.tick - first.tick
        rate = None if elapsed <= 0 or delta <= 0 else delta * 60.0 / elapsed
        return {
            "tick": last.tick,
            "ticks_per_minute": rate,
            "observed_ticks": max(0, delta),
            "observed_seconds": max(0.0, elapsed),
        }

    def forget(self, run_name: str) -> None:
        self._runs.pop(run_name, None)

    def _trim(self, samples: deque[TickObservation], now: float) -> None:
        cutoff = now - self.window_seconds
        # Retain one sample before the window when possible; it makes the first
        # delta inside a sparse stream meaningful without allowing growth.
        while len(samples) > 2 and samples[1].monotonic_seconds < cutoff:
            samples.popleft()


# Explicit alternative name for callers that treat this as an observations store.
ObservationTracker = TickRateTracker


__all__ = ["ObservationTracker", "TickObservation", "TickRateTracker"]
