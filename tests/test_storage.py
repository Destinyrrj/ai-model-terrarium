from __future__ import annotations

import json
import os
import sqlite3
import stat
from itertools import pairwise
from pathlib import Path

import pytest

from terrarium.events import (
    GENESIS_HASH,
    Event,
    EventValidationError,
    canonical_json,
    strict_json_loads,
)
from terrarium.storage import (
    EVENT_LOG_NAME,
    EventStore,
    RawResponseError,
    StorageError,
    StorageIntegrityError,
    StoreLockedError,
    sanitize_control_text,
)


def draft(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {"type": event_type, "payload": payload}


def test_canonical_json_and_event_hash_are_stable() -> None:
    left = {"z": [3, 2, 1], "a": {"snow": "雪", "ok": True}}
    right = {"a": {"ok": True, "snow": "雪"}, "z": [3, 2, 1]}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json({"z": (3, 2, 1)}) == canonical_json({"z": [3, 2, 1]})

    event = Event.create(
        run_id="run-1",
        seq=0,
        tick=0,
        type="fact",
        payload=left,
        prev_hash=GENESIS_HASH,
    )
    parsed = Event.from_dict(strict_json_loads(event.to_json()))
    assert parsed == event

    with pytest.raises(EventValidationError, match="non-finite"):
        canonical_json({"bad": float("nan")})
    with pytest.raises(EventValidationError, match="duplicate"):
        strict_json_loads('{"x":1,"x":2}')


def test_commit_checkpoint_hash_chain_and_sqlite_settings(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    state = {"hp": {"a": 90}, "rng_state": {"bit_generator": "opaque", "state": [1, 2]}}
    with EventStore(run_dir, "integrity-run") as store:
        transaction = store.commit_tick(
            0,
            [
                draft(
                    "agent_spawned",
                    {"agent_id": "a", "generation_id": "g0", "model": "mock"},
                ),
                draft(
                    "action",
                    {"action_id": "act-1", "agent_id": "a", "type": "forage"},
                ),
            ],
            state,
        )
        assert [event.type for event in transaction] == [
            "tick_begin",
            "agent_spawned",
            "action",
            "state_checkpoint",
            "tick_commit",
        ]
        for previous, current in pairwise(transaction):
            assert current.prev_hash == previous.hash
        checkpoint = store.load_latest_checkpoint()
        assert checkpoint is not None
        assert checkpoint["state"] == state
        assert checkpoint["rng_state"] == state["rng_state"]

        verification = store.verify()
        assert verification["committed_ticks"] == 1
        assert verification["committed_events"] == 5

        connection = sqlite3.connect(run_dir / "state.sqlite3")
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {
                "events",
                "checkpoints",
                "agents",
                "generations",
                "legacies",
                "actions",
                "deaths",
                "surveys",
                "token_usage",
                "meta",
            } <= tables
        finally:
            connection.close()

    log_lines = (run_dir / EVENT_LOG_NAME).read_bytes().splitlines()
    assert len(log_lines) == 5
    assert all(json.loads(line)["run_id"] == "integrity-run" for line in log_lines)


def test_recovery_discards_partial_physical_tail_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "crash-run") as store:
        store.commit_tick(0, [], {"value": 1})
        committed_size = (run_dir / EVENT_LOG_NAME).stat().st_size

    partial = b'{"schema_version":1,"run_id":"crash-run"'
    with (run_dir / EVENT_LOG_NAME).open("ab") as stream:
        stream.write(partial)
        stream.flush()
        os.fsync(stream.fileno())

    with EventStore(run_dir, "crash-run") as recovered:
        assert (run_dir / EVENT_LOG_NAME).stat().st_size == committed_size
        artifacts = list((run_dir / "recovery").glob("incomplete-tail-*.bin"))
        assert len(artifacts) == 1
        assert artifacts[0].read_bytes() == partial
        assert stat.S_IMODE(artifacts[0].stat().st_mode) == 0o600
        recovered.commit_tick(1, [], {"value": 2})
        assert recovered.verify()["committed_ticks"] == 2


def test_recovery_discards_complete_but_uncommitted_tick(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "crash-run") as store:
        store.commit_tick(4, [], {"value": 1})
        report = store.verify()
        committed_size = (run_dir / EVENT_LOG_NAME).stat().st_size

    begin = Event.create(
        run_id="crash-run",
        seq=report["last_seq"] + 1,
        tick=5,
        type="tick_begin",
        payload={"event_count": 1},
        prev_hash=report["last_hash"],
    )
    user_event = Event.create(
        run_id="crash-run",
        seq=begin.seq + 1,
        tick=5,
        type="fact",
        payload={"x": 1},
        prev_hash=begin.hash,
    )
    with (run_dir / EVENT_LOG_NAME).open("ab") as stream:
        stream.write((begin.to_json() + "\n" + user_event.to_json() + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())

    with EventStore(run_dir, "crash-run") as recovered:
        assert (run_dir / EVENT_LOG_NAME).stat().st_size == committed_size
        assert recovered.verify()["committed_ticks"] == 1
        recovered.commit_tick(5, [], {"value": 2})


def test_tampered_committed_event_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "tamper-run") as store:
        store.commit_tick(0, [draft("fact", {"safe": "yes"})], {"value": 1})
        store.commit_tick(1, [], {"value": 2})

    path = run_dir / EVENT_LOG_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(lines[1])
    envelope["payload"]["safe"] = "no"
    # Deliberately retain the old digest.
    lines[1] = canonical_json(envelope)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(StorageIntegrityError, match="hash mismatch"):
        EventStore(run_dir, "tamper-run")


def test_protocol_corruption_in_middle_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "protocol-run") as store:
        store.commit_tick(0, [], {"value": 1})
        store.commit_tick(1, [], {"value": 2})

    path = run_dir / EVENT_LOG_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    # A well-formed but unexpected blank record is not an incomplete tail.
    lines.insert(2, "{}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(StorageIntegrityError):
        EventStore(run_dir, "protocol-run")


def test_sqlite_projection_is_idempotent_and_rebuildable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "rebuild-run") as store:
        store.commit_tick(
            0,
            [
                draft(
                    "legacy_created",
                    {
                        "legacy_id": "legacy-1",
                        "author_agent_id": "a",
                        "generation_id": "g0",
                        "channel": "written",
                        "text": "Remember the rain.",
                        "parent_legacy_ids": [],
                    },
                ),
                draft(
                    "token_usage",
                    {
                        "agent_id": "a",
                        "provider": "mock",
                        "model": "mock-1",
                        "input_tokens": 12,
                        "output_tokens": 3,
                    },
                ),
            ],
            {"value": 1, "rng_state": {"opaque": True}},
        )

        # Reopening projects every committed transaction again; explicit rebuild
        # must also preserve one row per source event.
        store.rebuild_sqlite()
        store.rebuild_sqlite()
        assert store.verify()["sqlite"] == "ok"

        connection = sqlite3.connect(run_dir / "state.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) FROM legacies").fetchone() == (1,)
            assert connection.execute("SELECT COUNT(*) FROM token_usage").fetchone() == (1,)
            connection.execute("UPDATE events SET hash = ? WHERE seq = 0", ("f" * 64,))
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(StorageIntegrityError, match="differs"):
            store.verify()
        store.rebuild_sqlite()
        assert store.verify()["sqlite"] == "ok"

    with EventStore(run_dir, "rebuild-run") as reopened:
        assert reopened.verify()["sqlite"] == "ok"


def test_legacy_rows_are_immutable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "legacy-run") as store:
        store.commit_tick(
            0,
            [
                draft(
                    "legacy_created",
                    {
                        "legacy_id": "l1",
                        "text": "Do not edit me.",
                        "channel": "written",
                        "parent_legacy_ids": [],
                    },
                )
            ],
            {},
        )
        connection = sqlite3.connect(run_dir / "state.sqlite3")
        try:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE legacies SET text = ? WHERE legacy_id = ?", ("changed", "l1")
                )
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute("DELETE FROM legacies WHERE legacy_id = ?", ("l1",))
        finally:
            connection.close()


def test_verify_detects_semantically_forged_projection_rows(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "projection-tamper") as store:
        store.commit_tick(
            0,
            [
                draft(
                    "action",
                    {
                        "agent_id": "a",
                        "type": "forage",
                        "valid": True,
                    },
                )
            ],
            {"value": 1},
        )
        connection = sqlite3.connect(run_dir / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE actions SET action_type = ?, data_json = ?",
                ("eat", '{"forged":true}'),
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(StorageIntegrityError, match=r"actions.*differs"):
            store.verify()
        store.rebuild_sqlite()
        assert store.verify()["sqlite"] == "ok"


def test_reopen_and_checkpoint_load_reject_projection_forgery(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    state = {"tick": 0, "rng_state": {"state": "sealed"}}
    with EventStore(run_dir, "resume-tamper") as store:
        store.commit_tick(
            0,
            [draft("action", {"agent_id": "a", "type": "noop"})],
            state,
        )
        connection = sqlite3.connect(run_dir / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE checkpoints SET rng_state_json = ?",
                ('{"state":"forged"}',),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(StorageIntegrityError, match="RNG differs"):
            store.load_latest_checkpoint()

    connection = sqlite3.connect(run_dir / "state.sqlite3")
    try:
        connection.execute(
            "UPDATE actions SET action_type = ?, data_json = ?",
            ("dig", '{"forged":true}'),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(StorageIntegrityError, match="differs"):
        EventStore(run_dir, "resume-tamper")


def test_single_writer_lock(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    first = EventStore(run_dir, "locked-run")
    try:
        with pytest.raises(StoreLockedError):
            EventStore(run_dir, "locked-run")
    finally:
        first.close()
    with EventStore(run_dir, "locked-run"):
        pass


def test_raw_response_path_is_opaque_atomic_and_private(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "raw-run", max_raw_bytes=4096) as store:
        stored = store.append_raw(
            "../../generation",
            "../agent/../../../escape",
            7,
            {"stdout": "\x1b[31mRED\x1b[0m", "reasoning": "kept verbatim"},
            response_id="../../also-opaque",
        )
        assert stored.parent == (run_dir / "raw").resolve()
        assert stored.name.endswith(".json")
        assert ".." not in stored.name
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        record = json.loads(stored.read_text(encoding="utf-8"))
        assert record["agent"] == "../agent/../../../escape"
        assert record["response"]["stdout"] == "\x1b[31mRED\x1b[0m"
        with pytest.raises(RawResponseError, match="already exists"):
            store.append_raw(
                "../../generation",
                "../agent/../../../escape",
                7,
                {"different": True},
                response_id="../../also-opaque",
            )

    assert list(tmp_path.glob("escape*")) == []


def test_untrusted_values_never_become_sql_and_controls_are_sanitized(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    attack = "x'); DROP TABLE events; --"
    with EventStore(run_dir, "sql-run") as store:
        store.commit_tick(
            0,
            [draft("fact", {"agent": attack, "text": "\x1b[2Jhello\x00\u202eworld"})],
            {"note": attack},
        )
        assert store.verify()["sqlite"] == "ok"
        connection = sqlite3.connect(run_dir / "state.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (4,)
        finally:
            connection.close()

    assert sanitize_control_text("\x1b[31mred\x1b[0m\x00\u202etxt\n") == "redtxt\n"
    assert sanitize_control_text("abcdef", max_length=3) == "abc"


def test_bad_tick_state_and_reserved_events_do_not_touch_log(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with EventStore(run_dir, "validation-run") as store:
        before = (run_dir / EVENT_LOG_NAME).stat().st_size
        with pytest.raises(StorageError, match="reserved"):
            store.commit_tick(0, [draft("tick_commit", {})], {})
        with pytest.raises(StorageError, match="rng_state"):
            store.commit_tick(0, [], {"rng_state": "not an object"})
        assert (run_dir / EVENT_LOG_NAME).stat().st_size == before
