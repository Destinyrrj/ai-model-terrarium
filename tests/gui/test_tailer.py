from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from terrarium.events import GENESIS_HASH, Event, json_sha256
from terrarium.storage import EventStore
from terrarium_gui.tailer import (
    EventBroadcaster,
    EventLogTailer,
    TailerError,
    read_committed_events,
)


def _event(
    *,
    run_id: str,
    seq: int,
    tick: int,
    event_type: str,
    payload: Mapping[str, object],
    prev_hash: str,
) -> Event:
    return Event.create(
        run_id=run_id,
        seq=seq,
        tick=tick,
        type=event_type,
        payload=payload,
        prev_hash=prev_hash,
    )


def _tick(
    start_seq: int,
    tick: int,
    *,
    run_id: str = "demo",
    prev_hash: str = GENESIS_HASH,
    user_events: Sequence[tuple[str, Mapping[str, object]]] = (
        ("effect", {"agent_id": "a"}),
    ),
) -> tuple[bytes, tuple[Event, ...]]:
    events: list[Event] = []

    def add(event_type: str, payload: Mapping[str, object]) -> Event:
        event = _event(
            run_id=run_id,
            seq=start_seq + len(events),
            tick=tick,
            event_type=event_type,
            payload=payload,
            prev_hash=prev_hash if not events else events[-1].hash,
        )
        events.append(event)
        return event

    begin = add("tick_begin", {"event_count": len(user_events)})
    for event_type, payload in user_events:
        add(event_type, payload)
    state: dict[str, object] = {"tick": tick, "rng_state": {}}
    state_hash = json_sha256(state)
    checkpoint = add(
        "state_checkpoint",
        {"state": state, "rng_state": {}, "state_hash": state_hash},
    )
    add(
        "tick_commit",
        {
            "begin_seq": begin.seq,
            "event_count": len(user_events),
            "checkpoint_hash": state_hash,
            "checkpoint_event_hash": checkpoint.hash,
        },
    )
    return b"".join(event.to_json().encode() + b"\n" for event in events), tuple(events)


def _write_events(path: Path, events: Sequence[Event]) -> None:
    path.write_bytes(b"".join(event.to_json().encode() + b"\n" for event in events))


def test_tailer_emits_only_verified_commits_and_discards_recovered_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    first_bytes, first = _tick(0, 0)
    second_bytes, second = _tick(4, 1, prev_hash=first[-1].hash)
    path.write_bytes(first_bytes)
    tailer = EventLogTailer(path, projector=None)

    assert [item["seq"] for item in tailer.poll()] == [0, 1, 2, 3]
    committed_size = tailer.committed_offset

    second_lines = second_bytes.splitlines(keepends=True)
    with path.open("ab") as stream:
        stream.write(b"".join(second_lines[:2]))
    assert tailer.poll() == []
    assert tailer.committed_offset == committed_size

    # EventStore recovery can remove only the uncommitted suffix.  Re-reading
    # from the cryptographically verified boundary cannot emit the old bytes.
    with path.open("r+b") as stream:
        stream.truncate(committed_size)
        stream.seek(committed_size)
        stream.write(second_bytes)
    assert [item["seq"] for item in tailer.poll()] == [4, 5, 6, 7]
    assert tailer.last_hash == second[-1].hash


def test_tailer_rescans_rotation_and_requires_previous_exact_anchor(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first_bytes, first = _tick(0, 0)
    second_bytes, _second = _tick(4, 1, prev_hash=first[-1].hash)
    path.write_bytes(first_bytes)
    tailer = EventLogTailer(path, projector=None)
    assert len(tailer.poll()) == 4

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(first_bytes + second_bytes)
    os.replace(replacement, path)
    assert [item["seq"] for item in tailer.poll()] == [4, 5, 6, 7]
    assert tailer.reset_count == 1

    unrelated, _ = _tick(0, 0, run_id="other")
    replacement.write_bytes(unrelated)
    os.replace(replacement, path)
    with pytest.raises(TailerError, match="requested commit anchor"):
        tailer.poll()


def test_nested_begin_is_integrity_error_instead_of_resynchronization(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    begin = _event(
        run_id="demo",
        seq=0,
        tick=0,
        event_type="tick_begin",
        payload={"event_count": 1},
        prev_hash=GENESIS_HASH,
    )
    user = _event(
        run_id="demo",
        seq=1,
        tick=0,
        event_type="effect",
        payload={"reason": "stale"},
        prev_hash=begin.hash,
    )
    nested = _event(
        run_id="demo",
        seq=2,
        tick=0,
        event_type="tick_begin",
        payload={"event_count": 0},
        prev_hash=user.hash,
    )
    _write_events(path, (begin, user, nested))
    with pytest.raises(TailerError, match="nested tick_begin"):
        EventLogTailer(path, projector=None).poll()


def test_live_prime_verifies_history_and_keeps_uncommitted_suffix(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first_bytes, first = _tick(0, 0)
    second_bytes, _second = _tick(4, 1, prev_hash=first[-1].hash)
    second_lines = second_bytes.splitlines(keepends=True)
    path.write_bytes(first_bytes + b"".join(second_lines[:2]))
    tailer = EventLogTailer(path, projector=None)
    tailer.prime_to_latest_commit()

    assert tailer.last_seq == 3
    assert tailer.committed_offset == len(first_bytes)
    with path.open("ab") as stream:
        stream.write(b"".join(second_lines[2:]))

    assert [event["seq"] for event in tailer.poll()] == [4, 5, 6, 7]


def test_eventstore_stream_and_incremental_live_tail_pass_full_verification(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    with EventStore(run, "verified-run") as store:
        first = store.commit_tick(
            0,
            [{"type": "effect", "payload": {"agent_id": "a"}}],
            {"tick": 0, "rng_state": {}},
        )
        tailer = EventLogTailer(run / "events.jsonl", projector=None)
        assert [item["seq"] for item in tailer.poll()] == [0, 1, 2, 3]
        second = store.commit_tick(1, [], {"tick": 1, "rng_state": {}})
        assert [item["seq"] for item in tailer.poll()] == [4, 5, 6]

    assert tailer.last_hash == second[-1].hash
    anchored = read_committed_events(
        run / "events.jsonl",
        after_seq=first[-1].seq,
        projector=None,
        anchor=(first[-1].seq, first[-1].hash),
        require_anchor=True,
    )
    assert [item["seq"] for item in anchored] == [4, 5, 6]


def test_fake_hash_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _data, events = _tick(0, 0)
    envelope = events[0].to_dict()
    envelope["hash"] = "f" * 64
    path.write_text(json.dumps(envelope, separators=(",", ":")) + "\n")
    with pytest.raises(TailerError, match="invalid event record"):
        EventLogTailer(path, projector=None).poll()


@pytest.mark.parametrize("defect", ["sequence", "run_id"])
def test_chain_sequence_and_run_identity_discontinuities_are_rejected(
    tmp_path: Path,
    defect: str,
) -> None:
    path = tmp_path / "events.jsonl"
    begin = _event(
        run_id="demo",
        seq=0,
        tick=0,
        event_type="tick_begin",
        payload={"event_count": 1},
        prev_hash=GENESIS_HASH,
    )
    user = _event(
        run_id="other" if defect == "run_id" else "demo",
        seq=2 if defect == "sequence" else 1,
        tick=0,
        event_type="effect",
        payload={"agent_id": "a"},
        prev_hash=begin.hash,
    )
    _write_events(path, (begin, user))
    with pytest.raises(TailerError):
        EventLogTailer(path, projector=None).poll()


def test_prev_hash_discontinuity_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    begin = _event(
        run_id="demo",
        seq=0,
        tick=0,
        event_type="tick_begin",
        payload={"event_count": 1},
        prev_hash=GENESIS_HASH,
    )
    user = _event(
        run_id="demo",
        seq=1,
        tick=0,
        event_type="effect",
        payload={"agent_id": "a"},
        prev_hash="1" * 64,
    )
    _write_events(path, (begin, user))
    with pytest.raises(TailerError, match="broken prev_hash"):
        EventLogTailer(path, projector=None).poll()


def test_declared_count_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    begin = _event(
        run_id="demo",
        seq=0,
        tick=0,
        event_type="tick_begin",
        payload={"event_count": 0},
        prev_hash=GENESIS_HASH,
    )
    user = _event(
        run_id="demo",
        seq=1,
        tick=0,
        event_type="effect",
        payload={"agent_id": "a"},
        prev_hash=begin.hash,
    )
    _write_events(path, (begin, user))
    with pytest.raises(TailerError, match="more user events"):
        EventLogTailer(path, projector=None).poll()


def test_commit_attestation_cannot_spoof_an_exact_anchor(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _data, events = _tick(0, 0)
    checkpoint = events[-2]
    forged_commit = _event(
        run_id="demo",
        seq=events[-1].seq,
        tick=0,
        event_type="tick_commit",
        payload={
            "begin_seq": 0,
            "event_count": 1,
            "checkpoint_hash": checkpoint.payload["state_hash"],
            "checkpoint_event_hash": "f" * 64,
        },
        prev_hash=checkpoint.hash,
    )
    _write_events(path, (*events[:-1], forged_commit))

    with pytest.raises(TailerError, match="does not attest"):
        EventLogTailer(path, projector=None).position_after_commit(
            forged_commit.seq,
            forged_commit.hash,
        )


def test_checkpoint_state_hash_mismatch_is_rejected_even_with_matching_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    _data, events = _tick(0, 0)
    begin, user = events[:2]
    state: dict[str, object] = {"tick": 0, "rng_state": {}}
    forged_checkpoint = _event(
        run_id="demo",
        seq=2,
        tick=0,
        event_type="state_checkpoint",
        payload={"state": state, "rng_state": {}, "state_hash": "f" * 64},
        prev_hash=user.hash,
    )
    forged_commit = _event(
        run_id="demo",
        seq=3,
        tick=0,
        event_type="tick_commit",
        payload={
            "begin_seq": begin.seq,
            "event_count": 1,
            "checkpoint_hash": "f" * 64,
            "checkpoint_event_hash": forged_checkpoint.hash,
        },
        prev_hash=forged_checkpoint.hash,
    )
    _write_events(path, (begin, user, forged_checkpoint, forged_commit))
    with pytest.raises(TailerError, match="state_hash mismatch"):
        EventLogTailer(path, projector=None).poll()


def test_failed_poll_does_not_advance_verified_boundary(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first_bytes, first = _tick(0, 0)
    second_bytes, second = _tick(4, 1, prev_hash=first[-1].hash)
    path.write_bytes(first_bytes)
    tailer = EventLogTailer(path, projector=None)
    assert len(tailer.poll()) == 4

    with path.open("ab") as stream:
        stream.write(second_bytes + b"{}\n")
    with pytest.raises(TailerError, match="invalid event record"):
        tailer.poll()
    assert tailer.last_seq == 3
    assert tailer.last_hash == first[-1].hash

    path.write_bytes(first_bytes + second_bytes)
    assert [item["seq"] for item in tailer.poll()] == [4, 5, 6, 7]
    assert tailer.last_hash == second[-1].hash


def test_reverse_page_is_bounded_and_returns_nearest_events_before_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    chunks: list[bytes] = []
    prev_hash = GENESIS_HASH
    start_seq = 0
    for tick in range(3):
        chunk, events = _tick(
            start_seq,
            tick,
            prev_hash=prev_hash,
            user_events=(("opaque_event", {"hidden": True}),),
        )
        chunks.append(chunk)
        start_seq += len(events)
        prev_hash = events[-1].hash
    path.write_bytes(b"".join(chunks))

    page = read_committed_events(path, before_seq=11, limit=4)
    assert [item["seq"] for item in page] == [6, 7, 8, 10]


@pytest.mark.asyncio
async def test_broadcaster_signals_gap_to_slow_subscriber() -> None:
    broadcaster = EventBroadcaster(queue_size=2)
    queue = broadcaster.subscribe()
    await broadcaster.publish_many(
        {"seq": seq, "tick": 0, "type": "effect", "payload": {}} for seq in range(3)
    )
    item = await queue.get()
    assert item["kind"] == "gap"
    assert item["latest_seq"] == 2
