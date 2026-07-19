from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from terrarium.events import GENESIS_HASH, Event, json_sha256
from terrarium.storage import EventStore
from terrarium_gui.api.telemetry import create_telemetry_router
from terrarium_gui.registry import RunRegistry


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
    start: int,
    tick: int,
    *,
    run_id: str = "demo",
    prev_hash: str = GENESIS_HASH,
) -> tuple[bytes, tuple[Event, ...]]:
    begin = _event(
        run_id=run_id,
        seq=start,
        tick=tick,
        event_type="tick_begin",
        payload={"event_count": 0},
        prev_hash=prev_hash,
    )
    state: dict[str, object] = {"tick": tick, "rng_state": {}}
    state_hash = json_sha256(state)
    checkpoint = _event(
        run_id=run_id,
        seq=start + 1,
        tick=tick,
        event_type="state_checkpoint",
        payload={"state": state, "rng_state": {}, "state_hash": state_hash},
        prev_hash=begin.hash,
    )
    commit = _event(
        run_id=run_id,
        seq=start + 2,
        tick=tick,
        event_type="tick_commit",
        payload={
            "begin_seq": start,
            "event_count": 0,
            "checkpoint_hash": state_hash,
            "checkpoint_event_hash": checkpoint.hash,
        },
        prev_hash=checkpoint.hash,
    )
    events = (begin, checkpoint, commit)
    return b"".join(event.to_json().encode() + b"\n" for event in events), events


@pytest.mark.asyncio
async def test_sse_last_event_id_backfills_only_newer_commits(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "demo"
    run.mkdir(parents=True)
    first_bytes, first = _tick(0, 0)
    second_bytes, _ = _tick(3, 1, prev_hash=first[-1].hash)
    (run / "events.jsonl").write_bytes(first_bytes + second_bytes)
    router = create_telemetry_router(lambda _name: run, poll_interval=0.01)
    route = next(item for item in router.routes if item.path.endswith("/stream"))

    async def disconnected() -> dict[str, object]:
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/runs/demo/stream",
            "headers": [],
            "query_string": b"",
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        },
        receive=disconnected,
    )
    response = await route.endpoint("demo", request, "2")
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)
    assert "id: 0\n" not in body
    assert "id: 3\n" in body
    assert "id: 5\n" in body
    assert "event: tick\n" in body
    await router.close_telemetry()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sse_large_backfill_gap_reports_durable_latest_seq(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "demo"
    run.mkdir(parents=True)
    chunks: list[bytes] = []
    prev_hash = GENESIS_HASH
    for tick in range(5):
        chunk, transaction = _tick(tick * 3, tick, prev_hash=prev_hash)
        chunks.append(chunk)
        prev_hash = transaction[-1].hash
    (run / "events.jsonl").write_bytes(b"".join(chunks))
    router = create_telemetry_router(
        lambda _name: run,
        poll_interval=0.01,
        max_backfill=3,
    )
    route = next(item for item in router.routes if item.path.endswith("/stream"))

    async def disconnected() -> dict[str, object]:
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/runs/demo/stream",
            "headers": [],
            "query_string": b"",
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        },
        receive=disconnected,
    )
    response = await route.endpoint("demo", request, "-1")
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: gap\n" in body
    assert '"latest_seq":14' in body
    assert "id: 0\n" not in body
    await router.close_telemetry()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sse_falls_back_to_jsonl_when_projection_lags_durable_head(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "demo"
    with EventStore(run, "lagged") as store:
        first = store.commit_tick(0, [], {})
    second_bytes, _ = _tick(
        3,
        1,
        run_id="lagged",
        prev_hash=first[-1].hash,
    )
    with (run / "events.jsonl").open("ab") as stream:
        stream.write(second_bytes)

    router = create_telemetry_router(lambda _name: run, poll_interval=0.01)
    route = next(item for item in router.routes if item.path.endswith("/stream"))

    async def disconnected() -> dict[str, object]:
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/runs/demo/stream",
            "headers": [],
            "query_string": b"",
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        },
        receive=disconnected,
    )
    response = await route.endpoint("demo", request, "2")
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "id: 3\n" in body
    assert "id: 4\n" in body
    assert "id: 5\n" in body
    await router.close_telemetry()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_visible_commit_stays_quarantined_if_writer_releases_without_projection(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    run = runs / "demo"
    store = EventStore(run, "durability-fence")
    first = store.commit_tick(0, [], {})
    second_bytes, _ = _tick(
        3,
        1,
        run_id="durability-fence",
        prev_hash=first[-1].hash,
    )
    with (run / "events.jsonl").open("ab", buffering=0) as stream:
        # Simulate bytes made visible by write(2) before the sealed writer's
        # fsync.  The projection deliberately remains at tick zero.
        stream.write(second_bytes)

    registry = RunRegistry(runs)
    router = create_telemetry_router(registry.resolve, poll_interval=0.01)
    stream_route = next(item for item in router.routes if item.path.endswith("/stream"))
    events_route = next(item for item in router.routes if item.path.endswith("/events"))

    async def connected() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/runs/demo/stream",
        "headers": [],
        "query_string": b"",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1),
        "scheme": "http",
        "app": SimpleNamespace(state=SimpleNamespace(run_registry=registry)),
    }
    request = Request(scope, receive=connected)
    response = await stream_route.endpoint("demo", request, "2")
    first_chunk = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0.05)
    assert not first_chunk.done(), "pre-fsync bytes leaked through SSE"

    store.close()  # lock release without the missing projection is not proof
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(first_chunk, timeout=1)

    rest_request = Request({**scope, "path": "/runs/demo/events"}, receive=connected)
    page = await events_route.endpoint(
        "demo",
        rest_request,
        after_seq=2,
        before_seq=None,
        limit=500,
        tail=False,
    )
    assert page["events"] == []
    await router.close_telemetry()  # type: ignore[attr-defined]
