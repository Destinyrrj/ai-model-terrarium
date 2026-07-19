from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import terrarium_gui.sqlite_snapshot as sqlite_snapshot
from terrarium.storage import EventStore
from terrarium_gui.event_cache import VerifiedEventCache
from terrarium_gui.readmodel import ReadModel
from terrarium_gui.tailer import EventLogTailer


def test_sqlite_snapshot_is_reused_across_readmodel_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    with EventStore(run, "snapshot-cache") as store:
        store.commit_tick(0, [], {})

    copies = 0
    original = sqlite_snapshot._copy_fd

    def counted_copy(source_fd: int, target: Path) -> None:
        nonlocal copies
        copies += 1
        original(source_fd, target)

    monkeypatch.setattr(sqlite_snapshot, "_copy_fd", counted_copy)
    model = ReadModel(run)
    assert model.event_head()[0] is not None
    first_query_copies = copies
    assert first_query_copies >= 1

    model.event_head()
    model.agent_count()
    model.latest_events(limit=2)
    assert copies == first_query_copies


def test_verified_event_head_scans_genesis_once_then_only_appended_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    with EventStore(run, "event-cache") as store:
        for tick in range(8):
            store.commit_tick(tick, [], {"tick": tick})
    projection_head = ReadModel(run).event_head()
    assert type(projection_head[0]) is int and isinstance(projection_head[1], str)

    starts: list[int] = []
    original = EventLogTailer._scan_fd

    def observed_scan(
        self: EventLogTailer, fd: int, **kwargs: Any
    ) -> Any:
        starts.append(int(kwargs["start_offset"]))
        return original(self, fd, **kwargs)

    monkeypatch.setattr(EventLogTailer, "_scan_fd", observed_scan)
    cache = VerifiedEventCache()
    cache.head(run / "events.jsonl", anchor=projection_head)  # type: ignore[arg-type]
    initial_calls = len(starts)
    assert starts.count(0) == 1

    cache.head(run / "events.jsonl", anchor=projection_head)  # type: ignore[arg-type]
    assert len(starts) > initial_calls
    assert starts.count(0) == 1
