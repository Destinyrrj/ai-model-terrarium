"""Read-only telemetry REST and server-sent-event routes."""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import os
import stat
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from terrarium.storage import EVENT_LOG_NAME, LOCK_NAME

from ..event_cache import VerifiedEventCache
from ..observations import TickRateTracker
from ..readmodel import ReadModel, ReadModelDataError, ReadModelError, ReadModelUnavailable
from ..registry import InvalidRunName, RunNotFound, validate_run_name
from ..tailer import (
    EventLogTailer,
    EventStreamHub,
    StreamItem,
    TailerError,
    read_committed_events,
)

type ResolvedRun = str | Path
type RunResolver = Callable[[str], ResolvedRun | Awaitable[ResolvedRun]]
type MeasurementProvider = Callable[[str, str], Awaitable[dict[str, object]]]
SQLITE_INT_MAX = (1 << 63) - 1


class TelemetryHubManager:
    """Own per-run live tail tasks and close them during application shutdown."""

    def __init__(self, *, poll_interval: float, queue_size: int) -> None:
        self.poll_interval = poll_interval
        self.queue_size = queue_size
        self._hubs: dict[Path, EventStreamHub] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        event_path: Path,
        *,
        anchor: tuple[int, str] | None = None,
        prime_latest: bool = True,
        projection_gated: bool = False,
        writer_active: Callable[[], bool] | None = None,
        unsafe_batch_observer: Callable[[list[StreamItem]], None] | None = None,
    ) -> tuple[EventStreamHub, asyncio.Queue[StreamItem]]:
        created = False
        async with self._lock:
            hub = self._hubs.get(event_path)
            if hub is None:
                hub = EventStreamHub(
                    event_path,
                    poll_interval=self.poll_interval,
                    queue_size=self.queue_size,
                    writer_active=writer_active,
                    unsafe_batch_observer=unsafe_batch_observer,
                )
                self._hubs[event_path] = hub
                created = True
            try:
                queue = await hub.subscribe(
                    anchor=anchor,
                    prime_latest=prime_latest,
                    projection_gated=projection_gated,
                )
            except BaseException:
                if created and self._hubs.get(event_path) is hub:
                    self._hubs.pop(event_path, None)
                await hub.close()
                raise
            return hub, queue

    async def unsubscribe(
        self,
        event_path: Path,
        hub: EventStreamHub,
        queue: asyncio.Queue[StreamItem],
    ) -> None:
        close = False
        async with self._lock:
            hub.unsubscribe(queue)
            if hub.broadcaster.subscriber_count == 0 and self._hubs.get(event_path) is hub:
                self._hubs.pop(event_path, None)
                close = True
        if close:
            await hub.close()

    async def close(self) -> None:
        async with self._lock:
            hubs = list(self._hubs.values())
            self._hubs.clear()
        if hubs:
            await asyncio.gather(*(hub.close() for hub in hubs), return_exceptions=True)


class ProjectionDurabilityFence:
    """Remember JSONL commits observed before their fsync proof was visible."""

    def __init__(self) -> None:
        self._required: dict[Path, tuple[int, str | None]] = {}
        self._lock = Lock()

    def require(self, path: Path, seq: int, event_hash: str | None) -> None:
        if type(seq) is not int or seq < 0:
            return
        key = path.absolute()
        with self._lock:
            current = self._required.get(key)
            if current is None or seq > current[0]:
                self._required[key] = (seq, event_hash)
            elif seq == current[0] and current[1] is None and event_hash is not None:
                self._required[key] = (seq, event_hash)

    def require_batch(self, path: Path, events: list[StreamItem]) -> None:
        if not events:
            return
        latest = max(events, key=_event_seq)
        event_hash = latest.get("hash")
        self.require(
            path,
            _event_seq(latest),
            event_hash if isinstance(event_hash, str) else None,
        )

    def pending(
        self,
        path: Path,
        projection_head: tuple[int | None, str | None],
    ) -> bool:
        key = path.absolute()
        with self._lock:
            required = self._required.get(key)
            if required is None:
                return False
            seq, event_hash = required
            head_seq, head_hash = projection_head
            covered = type(head_seq) is int and (
                head_seq > seq
                or (head_seq == seq and (event_hash is None or head_hash == event_hash))
            )
            if covered:
                self._required.pop(key, None)
                return False
            return True


def create_telemetry_router(
    run_resolver: RunResolver | object | None = None,
    *,
    poll_interval: float = 0.25,
    queue_size: int = 256,
    max_backfill: int = 2_000,
    measurement_provider: MeasurementProvider | None = None,
) -> APIRouter:
    """Create telemetry routes.

    The returned router intentionally has no URL prefix or authentication
    dependency.  The application includes it under ``/api/v1`` alongside the
    local bearer-token dependency.
    """

    if type(max_backfill) is not int or max_backfill < 1:
        raise ValueError("max_backfill must be a positive integer")
    router = APIRouter(tags=["telemetry"])
    hubs = TelemetryHubManager(poll_interval=poll_interval, queue_size=queue_size)
    durability_fence = ProjectionDurabilityFence()
    verified_events = VerifiedEventCache()
    rates = TickRateTracker()
    read_executor = ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="terrarium-telemetry-read",
    )
    read_slots = asyncio.Semaphore(4)
    close_lock = asyncio.Lock()
    executor_closed = False

    async def execute(operation: Callable[[], Any]) -> Any:
        async with read_slots:
            return await _run_dedicated(read_executor, operation)

    async def read(operation: Callable[[], Any]) -> Any:
        return await _read(
            operation,
            executor=read_executor,
            slots=read_slots,
        )

    async def close_telemetry() -> None:
        nonlocal executor_closed
        async with close_lock:
            if executor_closed:
                return
            await hubs.close()
            executor_closed = True
            # This is deliberately not asyncio's default executor.  Joining
            # the owned pool here avoids default-executor/AnyIO teardown races.
            read_executor.shutdown(wait=True, cancel_futures=True)

    # APIRouter event handlers are transferred to the owning FastAPI app on
    # include_router, guaranteeing that no polling task survives shutdown.
    router.add_event_handler("shutdown", close_telemetry)
    router.close_telemetry = close_telemetry  # type: ignore[attr-defined]

    async def resolve(run_name: str, request: Request) -> Path:
        return await _resolve_run(run_resolver, run_name, request)

    @router.get("/runs/{run_name}/events")
    async def events(
        run_name: str,
        request: Request,
        after_seq: Annotated[int, Query(ge=-1, le=SQLITE_INT_MAX)] = -1,
        before_seq: Annotated[int | None, Query(ge=0, le=SQLITE_INT_MAX)] = None,
        limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
        tail: Annotated[bool, Query()] = False,
    ) -> dict[str, object]:
        root = await resolve(run_name, request)
        if tail and after_seq != -1:
            raise HTTPException(status_code=400, detail="tail_requires_initial_cursor")
        if before_seq is not None and (tail or after_seq != -1):
            raise HTTPException(status_code=400, detail="before_cursor_conflict")
        try:
            if before_seq is not None:
                items = await execute(
                    lambda: _event_page(
                        root,
                        after_seq=-1,
                        before_seq=before_seq,
                        limit=limit + 1,
                        tail=False,
                        writer_active=lambda: _writer_active(request, root),
                        durability_fence=durability_fence,
                        verified_events=verified_events,
                    )
                )
            else:
                items = await execute(
                    lambda: _event_page(
                        root,
                        after_seq=after_seq,
                        limit=limit + 1,
                        tail=tail,
                        writer_active=lambda: _writer_active(request, root),
                        durability_fence=durability_fence,
                        verified_events=verified_events,
                    )
                )
        except TailerError as exc:
            raise HTTPException(status_code=503, detail="event_log_unavailable") from exc
        except ReadModelError as exc:
            raise HTTPException(status_code=503, detail="read_model_invalid") from exc
        has_more = len(items) > limit
        page = items[-limit:] if tail or before_seq is not None else items[:limit]
        next_after_seq = _last_seq(page, default=after_seq)
        return {
            "items": page,
            "events": page,
            "after_seq": after_seq,
            "before_seq": before_seq,
            "next_after_seq": next_after_seq,
            "previous_before_seq": _event_seq(page[0]) if page else before_seq,
            "has_more": has_more,
        }

    @router.get("/runs/{run_name}/stream")
    async def stream(
        run_name: str,
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after_seq: Annotated[int, Query(ge=-1, le=SQLITE_INT_MAX)] = -1,
    ) -> StreamingResponse:
        root = await resolve(run_name, request)
        cursor = _parse_last_event_id(last_event_id) if last_event_id else after_seq
        event_path = root / EVENT_LOG_NAME
        hub: EventStreamHub | None = None
        queue: asyncio.Queue[StreamItem] | None = None
        try:
            writer_active = await execute(lambda: _writer_active(request, root))
            anchor = (
                await execute(lambda: _projection_anchor(root, verified_events))
                if writer_active
                else None
            )
            hub, queue = await hubs.subscribe(
                event_path,
                anchor=anchor,
                prime_latest=not writer_active,
                projection_gated=writer_active,
                writer_active=lambda: _writer_active(request, root),
                unsafe_batch_observer=lambda items: durability_fence.require_batch(
                    event_path, items
                ),
            )
            backfill = await execute(
                lambda: _event_page(
                    root,
                    after_seq=cursor,
                    limit=max_backfill + 1,
                    tail=False,
                    writer_active=lambda: _writer_active(request, root),
                    durability_fence=durability_fence,
                    verified_events=verified_events,
                )
            )
        except (TailerError, ReadModelError) as exc:
            if hub is not None and queue is not None:
                await hubs.unsubscribe(event_path, hub, queue)
            raise HTTPException(status_code=503, detail="event_log_unavailable") from exc
        assert hub is not None and queue is not None

        async def generate():
            nonlocal cursor
            try:
                if len(backfill) > max_backfill:
                    durable_latest = hub.tailer.last_seq
                    yield _sse_gap(
                        cursor,
                        durable_latest if durable_latest is not None else cursor,
                    )
                else:
                    for event in backfill:
                        seq = _event_seq(event)
                        if seq <= cursor:
                            continue
                        cursor = seq
                        yield _sse_event(event)

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if item.get("kind") == "gap":
                        latest = item.get("latest_seq")
                        if type(latest) is int and not await _wait_until_visible(
                            request,
                            root,
                            latest,
                            None,
                            poll_interval=poll_interval,
                            durability_fence=durability_fence,
                            executor=read_executor,
                            slots=read_slots,
                        ):
                            break
                        yield _sse_gap(cursor, latest if type(latest) is int else cursor)
                        continue
                    seq = _event_seq(item)
                    if seq <= cursor:
                        continue
                    event_hash = item.get("hash")
                    if not await _wait_until_visible(
                        request,
                        root,
                        seq,
                        event_hash if isinstance(event_hash, str) else None,
                        poll_interval=poll_interval,
                        durability_fence=durability_fence,
                        executor=read_executor,
                        slots=read_slots,
                    ):
                        break
                    cursor = seq
                    yield _sse_event(item)
            finally:
                await hubs.unsubscribe(event_path, hub, queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/runs/{run_name}/agents")
    async def agents(
        run_name: str,
        request: Request,
        offset: Annotated[int, Query(ge=0, le=SQLITE_INT_MAX)] = 0,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 2_000,
    ) -> dict[str, object]:
        model = await _model(resolve, run_name, request)
        items = await read(lambda: model.list_agents(offset=offset, limit=limit))
        total = await read(model.agent_count)
        return {
            "agents": items,
            "page": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + len(items) < total,
            },
        }

    @router.get("/runs/{run_name}/lineage")
    async def lineage(
        run_name: str,
        request: Request,
        offset: Annotated[int, Query(ge=0, le=SQLITE_INT_MAX)] = 0,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 2_000,
    ) -> dict[str, object]:
        model = await _model(resolve, run_name, request)
        return await read(lambda: model.lineage(offset=offset, limit=limit))

    @router.get("/runs/{run_name}/legacies")
    async def legacies(
        run_name: str,
        request: Request,
        offset: Annotated[int, Query(ge=0, le=SQLITE_INT_MAX)] = 0,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 2_000,
    ) -> dict[str, object]:
        model = await _model(resolve, run_name, request)
        items = await read(lambda: model.list_legacies(offset=offset, limit=limit))
        total = await read(model.legacy_count)
        return {
            "legacies": items,
            "page": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + len(items) < total,
            },
        }

    @router.get("/runs/{run_name}/world")
    async def world(
        run_name: str,
        request: Request,
        tick: Annotated[int | None, Query(ge=0, le=SQLITE_INT_MAX)] = None,
    ) -> dict[str, object] | None:
        model = await _model(resolve, run_name, request)
        return await read(lambda: model.world(tick=tick))

    @router.get("/runs/{run_name}/ticks")
    async def ticks(
        run_name: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 2_000,
    ) -> dict[str, object]:
        model = await _model(resolve, run_name, request)
        result = await read(lambda: model.ticks(limit=limit))
        raw_ticks = result.get("ticks")
        if isinstance(raw_ticks, list) and raw_ticks:
            latest = raw_ticks[-1]
            if isinstance(latest, dict) and type(latest.get("tick")) is int:
                result["live_rate"] = rates.observe(run_name, latest["tick"])
        else:
            result["live_rate"] = rates.snapshot(run_name)
        return result

    @router.get("/runs/{run_name}/metrics/tokens")
    async def token_metrics(
        run_name: str,
        request: Request,
        after_tick: Annotated[int | None, Query(ge=-1, le=SQLITE_INT_MAX)] = None,
        limit_ticks: Annotated[int | None, Query(ge=1, le=10_000)] = None,
    ) -> dict[str, object]:
        model = await _model(resolve, run_name, request)
        if after_tick is not None and limit_ticks is not None:
            raise HTTPException(status_code=400, detail="conflicting_token_window")
        window = 2_000 if after_tick is None and limit_ticks is None else limit_ticks
        return await read(
            lambda: model.token_metrics(after_tick=after_tick, limit_ticks=window)
        )

    @router.get("/runs/{run_name}/metrics/budget")
    async def budget_metrics(run_name: str, request: Request) -> dict[str, object]:
        model = await _model(resolve, run_name, request)
        return await read(model.budget_metrics)

    @router.get("/runs/{run_name}/metrics/behavior")
    async def behavior_metrics(run_name: str, request: Request) -> dict[str, object]:
        await resolve(run_name, request)
        return await _measurement_response(measurement_provider, run_name, "behavior")

    @router.get("/runs/{run_name}/metrics/knowledge-survival")
    async def knowledge_survival_metrics(
        run_name: str, request: Request
    ) -> dict[str, object]:
        await resolve(run_name, request)
        return await _measurement_response(
            measurement_provider, run_name, "knowledge-survival"
        )

    return router


async def _measurement_response(
    provider: MeasurementProvider | None, run_name: str, metric: str
) -> dict[str, object] | JSONResponse:
    if provider is None:
        raise HTTPException(status_code=503, detail="measurement_service_unavailable")
    try:
        result = await provider(run_name, metric)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="measurement_unavailable") from exc
    if result.get("status") == "pending":
        return JSONResponse(result, status_code=202)
    return result


async def _model(
    resolve: Callable[[str, Request], Awaitable[Path]],
    run_name: str,
    request: Request,
) -> ReadModel:
    return ReadModel(await resolve(run_name, request))


async def _read(
    operation: Callable[[], Any],
    *,
    executor: ThreadPoolExecutor,
    slots: asyncio.Semaphore,
) -> Any:
    try:
        async with slots:
            return await _run_dedicated(executor, operation)
    except ReadModelUnavailable as exc:
        raise HTTPException(status_code=503, detail="read_model_unavailable") from exc
    except ReadModelError as exc:
        raise HTTPException(status_code=503, detail="read_model_invalid") from exc


async def _run_dedicated(
    executor: ThreadPoolExecutor,
    operation: Callable[[], Any],
) -> Any:
    """Run one read without using or shutting down asyncio's default pool."""

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, operation)
    try:
        # Timed waits also work around a Python 3.14 selector wake-up race seen
        # when an executor future is the loop's only pending source of work.
        while not future.done():
            await asyncio.wait((future,), timeout=0.05)
        return future.result()
    except BaseException:
        future.cancel()
        raise


async def _resolve_run(
    resolver: RunResolver | object | None,
    run_name: str,
    request: Request,
) -> Path:
    try:
        validate_run_name(run_name)
        selected = resolver
        if selected is None:
            selected = getattr(request.app.state, "run_registry", None)
        method = selected if callable(selected) else getattr(selected, "resolve", None)
        if not callable(method):
            raise RunNotFound(run_name)
        value = method(run_name)
        if inspect.isawaitable(value):
            value = await value
        root = _absolute_path(value)
    except (InvalidRunName, RunNotFound, FileNotFoundError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    return root


def _absolute_path(value: object) -> Path:
    return Path(value).absolute()  # type: ignore[arg-type]


def _parse_last_event_id(value: str | None) -> int:
    if value is None or value == "":
        return -1
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_last_event_id") from exc
    if not -1 <= parsed <= SQLITE_INT_MAX:
        raise HTTPException(status_code=400, detail="invalid_last_event_id")
    return parsed


def _event_seq(event: dict[str, object]) -> int:
    seq = event.get("seq")
    return seq if type(seq) is int else -1


def _last_seq(events: list[dict[str, object]], *, default: int) -> int:
    return max((_event_seq(event) for event in events), default=default)


def _event_page(
    root: Path,
    *,
    after_seq: int,
    before_seq: int | None = None,
    limit: int,
    tail: bool,
    writer_active: Callable[[], bool] | None = None,
    durability_fence: ProjectionDurabilityFence | None = None,
    verified_events: VerifiedEventCache | None = None,
) -> list[dict[str, object]]:
    lease_fd, fail_closed = _try_projection_read_lease(root)
    try:
        effective_probe = (
            (lambda: False)
            if lease_fd is not None
            else ((lambda: True) if fail_closed else writer_active)
        )
        return _event_page_with_lease(
            root,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            tail=tail,
            writer_active=effective_probe,
            durability_fence=durability_fence,
            verified_events=verified_events,
        )
    finally:
        if lease_fd is not None:
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_UN)
            finally:
                os.close(lease_fd)


def _event_page_with_lease(
    root: Path,
    *,
    after_seq: int,
    before_seq: int | None,
    limit: int,
    tail: bool,
    writer_active: Callable[[], bool] | None,
    durability_fence: ProjectionDurabilityFence | None,
    verified_events: VerifiedEventCache | None,
) -> list[dict[str, object]]:
    """Prefer the indexed read projection, falling back to authoritative JSONL."""

    model = ReadModel(root, busy_retries=0)
    event_path = root / EVENT_LOG_NAME

    def projection_page() -> list[dict[str, object]]:
        if before_seq is not None:
            return model.events_before(before_seq=before_seq, limit=limit)
        return model.latest_events(limit=limit) if tail else model.events(
            after_seq=after_seq,
            limit=limit,
        )

    active_during_read = bool(writer_active is not None and writer_active())
    projection_head: tuple[int | None, str | None] | None = None
    try:
        projection_head = model.event_head()
    except ReadModelUnavailable:
        projection_head = None

    projection_anchor = (
        (projection_head[0], projection_head[1])
        if projection_head is not None
        and type(projection_head[0]) is int
        and isinstance(projection_head[1], str)
        else None
    )
    durable_head = (
        verified_events.head(event_path, anchor=projection_anchor)
        if verified_events is not None
        else _uncached_event_head(event_path)
    )

    if projection_head is not None:
        active_during_read = active_during_read or bool(
            writer_active is not None and writer_active()
        )
        if active_during_read and projection_head != durable_head:
            if durability_fence is not None and type(durable_head[0]) is int:
                durability_fence.require(event_path, durable_head[0], durable_head[1])
        if active_during_read and not _projection_head_is_anchored(
            event_path, projection_head, verified_events
        ):
            raise ReadModelDataError("projection head is not an exact JSONL commit anchor")
        if durability_fence is not None and durability_fence.pending(
            event_path, projection_head
        ):
            return projection_page()
        if projection_head == durable_head:
            return projection_page()

    if durability_fence is not None and durability_fence.pending(
        event_path,
        projection_head if projection_head is not None else (None, None),
    ):
        return projection_page()

    active_during_read = active_during_read or bool(
        writer_active is not None and writer_active()
    )
    if active_during_read:
        # Bytes become visible before the sealed writer's fsync completes.
        # SQLite projection begins only after that fsync, so its head is the
        # durability witness while the writer lock is held.
        return projection_page()

    if before_seq is not None:
        return read_committed_events(
            event_path,
            before_seq=before_seq,
            limit=limit,
        )

    if (
        projection_head is not None
        and type(projection_head[0]) is int
        and isinstance(projection_head[1], str)
    ):
        projection_seq = projection_head[0]
        anchor = (projection_seq, projection_head[1])
        try:
            if tail:
                suffix = read_committed_events(
                    event_path,
                    after_seq=projection_seq,
                    limit=limit,
                    tail=True,
                    anchor=anchor,
                    require_anchor=True,
                )
                if len(suffix) >= limit:
                    return suffix[-limit:]
                prefix = model.latest_events(limit=limit - len(suffix))
                return [*prefix, *suffix]

            prefix = model.events(after_seq=after_seq, limit=limit)
            if len(prefix) >= limit:
                return prefix
            suffix = read_committed_events(
                event_path,
                after_seq=max(after_seq, projection_seq),
                limit=limit - len(prefix),
                anchor=anchor,
                require_anchor=True,
            )
            return [*prefix, *suffix]
        except (ReadModelUnavailable, TailerError):
            # A mismatched/corrupt projection anchor is never trusted.  The
            # authoritative fallback below remains bounded in memory.
            pass

    return read_committed_events(
        event_path,
        after_seq=after_seq,
        limit=limit,
        tail=tail,
    )


def _try_projection_read_lease(root: Path) -> tuple[int | None, bool]:
    """Hold the existing writer lock shared while selecting read sources."""

    path = root / LOCK_NAME
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            return None, True
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return None, True
        return fd, False
    except BaseException:
        os.close(fd)
        raise


def _projection_anchor(
    root: Path,
    verified_events: VerifiedEventCache | None = None,
) -> tuple[int, str] | None:
    try:
        seq, event_hash = ReadModel(root, busy_retries=0).event_head()
    except ReadModelError:
        return None
    if type(seq) is int and isinstance(event_hash, str):
        if not _projection_head_is_anchored(
            root / EVENT_LOG_NAME,
            (seq, event_hash),
            verified_events,
        ):
            raise ReadModelDataError("projection head is not an exact JSONL commit anchor")
        return seq, event_hash
    return None


def _projection_head_is_anchored(
    event_path: Path,
    head: tuple[int | None, str | None],
    verified_events: VerifiedEventCache | None = None,
) -> bool:
    seq, event_hash = head
    if seq is None and event_hash is None:
        return True
    if type(seq) is not int or not isinstance(event_hash, str):
        return False
    if verified_events is not None:
        return verified_events.contains_or_verify(event_path, (seq, event_hash))
    return EventLogTailer(event_path, projector=None).position_after_commit(seq, event_hash)


def _uncached_event_head(event_path: Path) -> tuple[int | None, str | None]:
    tailer = EventLogTailer(event_path, projector=None)
    tailer.prime_to_latest_commit()
    return tailer.last_seq, tailer.last_hash


def _writer_active(request: Request, root: Path) -> bool:
    app = request.scope.get("app")
    state = getattr(app, "state", None)
    registry = getattr(state, "run_registry", None)
    probe = getattr(registry, "writer_active", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(root))
    except OSError:
        # A failed safety probe cannot prove that visible bytes are durable.
        return True


async def _wait_until_visible(
    request: Request,
    root: Path,
    seq: int,
    event_hash: str | None,
    *,
    poll_interval: float,
    durability_fence: ProjectionDurabilityFence,
    executor: ThreadPoolExecutor,
    slots: asyncio.Semaphore,
) -> bool:
    event_path = root / EVENT_LOG_NAME
    durability_fence.require(event_path, seq, event_hash)
    while True:
        try:
            async with slots:
                projection_head = await _run_dedicated(
                    executor,
                    lambda: ReadModel(root, busy_retries=0).event_head(),
                )
        except ReadModelError:
            projection_head = (None, None)
        if not durability_fence.pending(event_path, projection_head):
            return True
        if not _writer_active(request, root):
            # The lock release is not an fsync proof.  Keep the in-memory
            # quarantine for REST/SSE reconnects until rebuild/resume advances
            # SQLite to this exact commit identity.
            return False
        if await request.is_disconnected():
            return False
        await asyncio.sleep(min(poll_interval, 0.1))
    return True


def _sse_event(event: dict[str, object]) -> str:
    seq = _event_seq(event)
    name = "tick" if event.get("type") == "tick_commit" else "event"
    data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {name}\ndata: {data}\n\n"


def _sse_gap(after_seq: int, latest_seq: int) -> str:
    data = json.dumps(
        {
            "type": "gap",
            "after_seq": after_seq,
            "latest_seq": latest_seq,
            "backfill": "rest",
        },
        separators=(",", ":"),
    )
    return f"event: gap\ndata: {data}\n\n"


__all__ = [
    "MeasurementProvider",
    "RunResolver",
    "TelemetryHubManager",
    "create_telemetry_router",
]
