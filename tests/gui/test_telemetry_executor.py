from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from starlette.requests import Request

from terrarium_gui.api import telemetry


def _request(path: str) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        },
        receive=receive,
    )


@pytest.mark.asyncio
async def test_blocked_telemetry_read_does_not_stall_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "demo"
    run.mkdir(parents=True)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def blocked_event_page(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=1.0)
        return []

    monkeypatch.setattr(telemetry, "_event_page", blocked_event_page)
    router = telemetry.create_telemetry_router(lambda _name: run)
    route = next(item for item in router.routes if item.path.endswith("/events"))
    task = asyncio.create_task(route.endpoint("demo", _request("/runs/demo/events")))
    safety_release = threading.Timer(1.0, release.set)
    safety_release.start()

    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)

        heartbeat_started = time.monotonic()
        await asyncio.sleep(0.02)
        assert time.monotonic() - heartbeat_started < 0.2
        assert not task.done()

        release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result["events"] == []
    finally:
        release.set()
        safety_release.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await router.close_telemetry()  # type: ignore[attr-defined]
        await router.close_telemetry()  # type: ignore[attr-defined]
