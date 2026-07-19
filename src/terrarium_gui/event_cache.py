"""Incremental verified JSONL heads shared by read-only GUI requests."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .tailer import EventLogTailer, StreamItem, TailerError


def _commit_identity(event: Mapping[str, object]) -> StreamItem | None:
    if event.get("type") != "tick_commit":
        return None
    seq = event.get("seq")
    event_hash = event.get("hash")
    if type(seq) is not int or not isinstance(event_hash, str):
        return None
    return {"seq": seq, "hash": event_hash}


@dataclass(slots=True)
class _Entry:
    tailer: EventLogTailer
    initialized: bool = False
    recent: deque[tuple[int, str]] = field(default_factory=lambda: deque(maxlen=4096))


class VerifiedEventCache:
    """Verify history once, then scan only bytes after the cached commit."""

    def __init__(self) -> None:
        self._entries: dict[Path, _Entry] = {}
        self._lock = threading.RLock()

    def head(
        self,
        path: str | Path,
        *,
        anchor: tuple[int, str] | None = None,
    ) -> tuple[int | None, str | None]:
        key = Path(path).absolute()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(EventLogTailer(key, projector=_commit_identity))
                self._entries[key] = entry
            if not entry.initialized:
                if anchor is not None:
                    if not entry.tailer.position_after_commit(*anchor):
                        raise TailerError(
                            "event log does not contain the projection commit anchor"
                        )
                    self._remember(entry, anchor)
                else:
                    entry.tailer.prime_to_latest_commit()
                    self._remember_head(entry)
                entry.initialized = True
            for commit in entry.tailer.poll():
                seq = commit.get("seq")
                event_hash = commit.get("hash")
                if type(seq) is int and isinstance(event_hash, str):
                    self._remember(entry, (seq, event_hash))
            self._remember_head(entry)
            return entry.tailer.last_seq, entry.tailer.last_hash

    def contains_or_verify(
        self,
        path: str | Path,
        anchor: tuple[int, str],
    ) -> bool:
        key = Path(path).absolute()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and anchor in entry.recent:
                return True
            verifier = EventLogTailer(key, projector=_commit_identity)
            if not verifier.position_after_commit(*anchor):
                return False
            if entry is None:
                entry = _Entry(verifier, initialized=True)
                self._entries[key] = entry
            self._remember(entry, anchor)
            return True

    def identity(
        self,
        path: str | Path,
        *,
        anchor: tuple[int, str] | None = None,
    ) -> tuple[int | None, int | None, str | None]:
        self.head(path, anchor=anchor)
        key = Path(path).absolute()
        with self._lock:
            tailer = self._entries[key].tailer
            return tailer.last_tick, tailer.last_seq, tailer.last_hash

    @staticmethod
    def _remember(entry: _Entry, identity: tuple[int, str]) -> None:
        if not entry.recent or entry.recent[-1] != identity:
            entry.recent.append(identity)

    def _remember_head(self, entry: _Entry) -> None:
        seq = entry.tailer.last_seq
        event_hash = entry.tailer.last_hash
        if type(seq) is int and isinstance(event_hash, str):
            self._remember(entry, (seq, event_hash))


__all__ = ["VerifiedEventCache"]
