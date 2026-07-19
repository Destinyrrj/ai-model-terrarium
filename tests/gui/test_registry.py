from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from terrarium.config import RunConfig, load_config
from terrarium.events import Event, json_sha256
from terrarium.manifest import RunManifest
from terrarium.storage import EventStore
from terrarium_gui.readmodel import ReadModel
from terrarium_gui.registry import RunRegistry


def _config(*, run_id: str, size: int = 2, generations: int = 3) -> RunConfig:
    base = load_config("configs/mvp.yaml")
    population = base.population.model_copy(
        update={"size": size, "generations": generations}
    )
    return base.model_copy(update={"run_id": run_id, "population": population})


def _sealed_run(
    root: Path,
    name: str,
    *,
    generations: list[int] | None,
    config: RunConfig | None = None,
) -> tuple[Path, RunConfig]:
    sealed = config or _config(run_id=f"{name}-id")
    run_dir = root / name
    state: dict[str, object] = {}
    if generations is not None:
        state["lineages"] = {
            f"lineage_{index}": {"generation": generation}
            for index, generation in enumerate(generations)
        }
    with EventStore(run_dir, sealed.run_id) as store:
        store.commit_tick(0, [], state)
    RunManifest.from_config(sealed).write_new(run_dir / "manifest.json")
    return run_dir, sealed


@pytest.mark.parametrize(
    ("lineage_generations", "expected_status", "expected_completed"),
    [
        ([2, 2], "completed", True),
        ([2], "resumable", False),
        ([2, 1], "resumable", False),
        ([2, 2, 2], "resumable", False),
    ],
)
def test_completion_requires_exact_configured_lineage_count(
    tmp_path: Path,
    lineage_generations: list[int],
    expected_status: str,
    expected_completed: bool,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _sealed_run(runs, "cardinality", generations=lineage_generations)

    record = RunRegistry(runs).inspect("cardinality")

    assert record.status == expected_status
    assert record.completed is expected_completed
    assert record.target_generation == 2


def test_missing_projection_is_incomplete_and_requests_rebuild(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir, _ = _sealed_run(runs, "missing-db", generations=[0, 0])
    (run_dir / "state.sqlite3").unlink()

    record = RunRegistry(runs).inspect("missing-db")

    assert record.status == "incomplete"
    assert record.error == "projection_missing"
    assert not record.completed


def test_empty_projection_is_not_resumable_without_committed_identity(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    config = _config(run_id="empty-projection-id")
    run_dir = runs / "empty-projection"
    with EventStore(run_dir, config.run_id):
        pass
    RunManifest.from_config(config).write_new(run_dir / "manifest.json")

    record = RunRegistry(runs).inspect("empty-projection")

    assert record.status == "incomplete"
    assert record.error == "no_committed_checkpoint"
    assert record.last_seq is None
    assert not record.completed


def test_projection_without_checkpoint_is_not_resumable(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir, _ = _sealed_run(runs, "missing-checkpoint", generations=[0, 0])
    connection = sqlite3.connect(run_dir / "state.sqlite3")
    try:
        connection.execute("DELETE FROM checkpoints")
        connection.commit()
    finally:
        connection.close()

    record = RunRegistry(runs).inspect("missing-checkpoint")

    assert record.status == "incomplete"
    assert record.error == "no_committed_checkpoint"
    assert record.last_seq is not None


def test_corrupt_projection_is_invalid_not_resumable(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir, _ = _sealed_run(runs, "corrupt-db", generations=[0, 0])
    (run_dir / "state.sqlite3").write_bytes(b"not a sqlite database")

    record = RunRegistry(runs).inspect("corrupt-db")

    assert record.status == "invalid"
    assert record.error == "projection_unavailable"
    assert not record.completed


def test_missing_authoritative_event_log_is_never_completed(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir, _ = _sealed_run(runs, "missing-events", generations=[2, 2])
    (run_dir / "events.jsonl").unlink()

    record = RunRegistry(runs).inspect("missing-events")

    assert record.status == "invalid"
    assert record.error == "event_log_missing"
    assert not record.completed


def test_unsafe_writer_lock_fails_closed(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir, _ = _sealed_run(runs, "unsafe-lock", generations=[0, 0])
    lock = run_dir / ".writer.lock"
    lock.unlink()
    lock.mkdir()

    assert RunRegistry.writer_active(run_dir)


def test_live_wal_reads_do_not_modify_any_run_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    config = _config(run_id="no-shm-writes")
    run_dir = runs / "live"
    store = EventStore(run_dir, config.run_id)
    try:
        store.commit_tick(0, [], {"lineages": {}})
        RunManifest.from_config(config).write_new(run_dir / "manifest.json")
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in run_dir.iterdir()
            if path.is_file()
        }

        assert ReadModel(run_dir).event_head()[0] is not None
        assert RunRegistry(runs).inspect("live").status == "external"

        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in run_dir.iterdir()
            if path.is_file()
        }
        assert after == before
    finally:
        store.close()


def test_stale_projection_uses_authoritative_jsonl_progress(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    config = _config(run_id="projection-stale")
    run_dir = runs / "stale"
    with EventStore(run_dir, config.run_id) as store:
        first = store.commit_tick(0, [], {"tick": 0, "rng_state": {}})
    RunManifest.from_config(config).write_new(run_dir / "manifest.json")

    state: dict[str, object] = {"tick": 1, "rng_state": {}}
    state_hash = json_sha256(state)
    begin = Event.create(
        run_id=config.run_id,
        seq=3,
        tick=1,
        type="tick_begin",
        payload={"event_count": 0},
        prev_hash=first[-1].hash,
    )
    checkpoint = Event.create(
        run_id=config.run_id,
        seq=4,
        tick=1,
        type="state_checkpoint",
        payload={"state": state, "rng_state": {}, "state_hash": state_hash},
        prev_hash=begin.hash,
    )
    commit = Event.create(
        run_id=config.run_id,
        seq=5,
        tick=1,
        type="tick_commit",
        payload={
            "begin_seq": 3,
            "event_count": 0,
            "checkpoint_hash": state_hash,
            "checkpoint_event_hash": checkpoint.hash,
        },
        prev_hash=checkpoint.hash,
    )
    with (run_dir / "events.jsonl").open("ab") as stream:
        for event in (begin, checkpoint, commit):
            stream.write(event.to_json().encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())

    record = RunRegistry(runs).inspect("stale")

    assert record.status == "resumable"
    assert record.error == "projection_stale"
    assert record.last_tick == 1
    assert record.last_seq == 5
    assert record.last_hash == commit.hash
    assert not record.completed
