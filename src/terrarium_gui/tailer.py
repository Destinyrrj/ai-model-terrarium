"""Read-only committed-event tailing and bounded fan-out.

The writer may leave a physical or fully-written uncommitted tail when it is
interrupted.  A tailer therefore advances its durable offset only when it sees a
matching ``tick_commit``.  Bytes after that boundary are re-read on every poll;
if recovery truncates or replaces them, no stale batch can leak to clients.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from terrarium.events import (
    GENESIS_HASH,
    HASH_RE,
    Event,
    EventValidationError,
    json_sha256,
    strict_json_loads,
)
from terrarium.storage import DEFAULT_MAX_EVENT_LINE_BYTES, RESERVED_EVENT_TYPES

from .sanitize import project_event

type EventProjector = Callable[[Mapping[str, object]], dict[str, object] | None]
type StreamItem = dict[str, object]
type WriterProbe = Callable[[], bool]
type UnsafeBatchObserver = Callable[[list[StreamItem]], None]

_READ_FLAGS: Final = (
    os.O_RDONLY
    | int(getattr(os, "O_CLOEXEC", 0))
    | int(getattr(os, "O_NOFOLLOW", 0))
    | int(getattr(os, "O_NONBLOCK", 0))
)


class TailerError(RuntimeError):
    """The event log cannot safely be consumed as a committed stream."""


@dataclass(frozen=True, slots=True)
class _VerifiedBoundary:
    """Cryptographic and transaction state at one committed byte boundary."""

    run_id: str | None = None
    next_seq: int = 0
    prev_hash: str = GENESIS_HASH
    last_tick: int | None = None


@dataclass(slots=True)
class _OpenTransaction:
    begin: Event
    expected_user_events: int
    events: list[Event] = field(default_factory=list)
    user_events: int = 0
    checkpoint: Event | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedScan:
    committed_offset: int
    boundary: _VerifiedBoundary
    emitted: tuple[StreamItem, ...]
    anchor_found: bool = False


class EventLogTailer:
    """Incrementally read complete tick transactions from one JSONL log.

    The object never opens SQLite or the writer lock and never mutates the run
    directory.  ``poll`` is synchronous by design; the local append-only read is
    short and the async hub controls its polling cadence.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        projector: EventProjector | None = project_event,
        max_line_bytes: int = DEFAULT_MAX_EVENT_LINE_BYTES,
    ) -> None:
        if type(max_line_bytes) is not int or max_line_bytes < 1:
            raise ValueError("max_line_bytes must be a positive integer")
        self.path = Path(path).absolute()
        self.projector = projector
        self.max_line_bytes = max_line_bytes
        self._committed_offset = 0
        self._identity: tuple[int, int] | None = None
        self._boundary = _VerifiedBoundary()
        self._reset_count = 0

    @property
    def committed_offset(self) -> int:
        return self._committed_offset

    @property
    def last_seq(self) -> int | None:
        return None if self._boundary.next_seq == 0 else self._boundary.next_seq - 1

    @property
    def last_hash(self) -> str | None:
        return None if self._boundary.next_seq == 0 else self._boundary.prev_hash

    @property
    def last_tick(self) -> int | None:
        return self._boundary.last_tick

    @property
    def reset_count(self) -> int:
        return self._reset_count

    def poll(self) -> list[StreamItem]:
        """Return newly committed, projected events in sequence order."""

        try:
            fd = os.open(self.path, _READ_FLAGS)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise TailerError("event log is unavailable") from exc

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TailerError("event log must be a private regular file")
            identity = (info.st_dev, info.st_ino)
            reset = self._identity is not None and (
                identity != self._identity or info.st_size < self._committed_offset
            )
            if reset:
                # A replaced/truncated log is accepted only if a full verified
                # rescan reaches the exact commit previously delivered.  This
                # both suppresses duplicates and prevents rotation from
                # silently switching the tailer to a different valid run.
                prior_seq = self.last_seq
                prior_hash = self.last_hash
                required_anchor = (
                    (prior_seq, prior_hash)
                    if type(prior_seq) is int and isinstance(prior_hash, str)
                    else None
                )
                scan = self._scan_fd(
                    fd,
                    start_offset=0,
                    boundary=_VerifiedBoundary(),
                    after_seq=-1 if prior_seq is None else prior_seq,
                    required_anchor=required_anchor,
                )
                self._apply_scan(scan, identity=identity)
                self._reset_count += 1
                return list(scan.emitted)
            scan = self._scan_fd(
                fd,
                start_offset=self._committed_offset,
                boundary=self._boundary,
            )
            self._apply_scan(scan, identity=identity)
            return list(scan.emitted)
        finally:
            os.close(fd)

    def read_available(self) -> list[StreamItem]:
        """Compatibility alias for :meth:`poll`."""

        return self.poll()

    def prime_to_latest_commit(self) -> None:
        """Position a fresh live tailer after the latest durable commit.

        Historical delivery is handled by the indexed REST read model.  The
        complete prefix is nevertheless verified before its head is trusted:
        a commit-looking reverse line is not a cryptographic proof.
        """

        try:
            fd = os.open(self.path, _READ_FLAGS)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TailerError("event log is unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TailerError("event log must be a private regular file")
            scan = self._scan_fd(
                fd,
                start_offset=0,
                boundary=_VerifiedBoundary(),
                collect=False,
            )
            self._apply_scan(scan, identity=(info.st_dev, info.st_ino))
        finally:
            os.close(fd)

    def position_after_commit(self, seq: int, event_hash: str) -> bool:
        """Position after an exact durable commit identity, if present."""

        if (
            type(seq) is not int
            or seq < 0
            or not isinstance(event_hash, str)
            or HASH_RE.fullmatch(event_hash) is None
        ):
            raise ValueError("commit anchor must contain a sequence and hash")
        try:
            fd = os.open(self.path, _READ_FLAGS)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise TailerError("event log is unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TailerError("event log must be a private regular file")
            scan = self._scan_fd(
                fd,
                start_offset=0,
                boundary=_VerifiedBoundary(),
                collect=False,
                required_anchor=(seq, event_hash),
                stop_at_anchor=True,
            )
            if not scan.anchor_found:
                return False
            self._apply_scan(scan, identity=(info.st_dev, info.st_ino))
            return True
        finally:
            os.close(fd)

    def _read_fd(
        self,
        fd: int,
        *,
        after_seq: int = -1,
        before_seq: int | None = None,
        limit: int | None = None,
        tail: bool = False,
    ) -> list[StreamItem]:
        scan = self._scan_fd(
            fd,
            start_offset=self._committed_offset,
            boundary=self._boundary,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            tail=tail,
        )
        self._apply_scan(scan)
        return list(scan.emitted)

    def _scan_fd(
        self,
        fd: int,
        *,
        start_offset: int,
        boundary: _VerifiedBoundary,
        after_seq: int = -1,
        before_seq: int | None = None,
        limit: int | None = None,
        tail: bool = False,
        collect: bool = True,
        required_anchor: tuple[int, str] | None = None,
        stop_at_anchor: bool = False,
    ) -> _VerifiedScan:
        """Verify complete records and return an atomic committed-state update."""

        reverse_page = tail or before_seq is not None
        emitted: list[StreamItem] | deque[StreamItem]
        emitted = deque(maxlen=limit) if reverse_page else []
        try:
            os.lseek(fd, start_offset, os.SEEK_SET)
        except OSError as exc:
            raise TailerError("event log seek failed") from exc

        committed_offset = start_offset
        committed_boundary = boundary
        scan_run_id = boundary.run_id
        expected_seq = boundary.next_seq
        expected_prev_hash = boundary.prev_hash
        open_tick: _OpenTransaction | None = None
        anchor_found = required_anchor is None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while True:
                line_start = stream.tell()
                line = stream.readline(self.max_line_bytes + 1)
                if not line:
                    break
                if len(line) > self.max_line_bytes:
                    raise TailerError("event line exceeds safety limit")
                if not line.endswith(b"\n"):
                    # Only a physical final tail may lack a newline.  It is not
                    # part of a committed transaction and remains unread.
                    break
                line_end = stream.tell()
                try:
                    item = strict_json_loads(line[:-1])
                    if not isinstance(item, dict):
                        raise EventValidationError("event line must contain a JSON object")
                    event = Event.from_dict(item)
                except EventValidationError as exc:
                    raise TailerError(f"invalid event record at byte {line_start}") from exc

                if scan_run_id is None:
                    scan_run_id = event.run_id
                elif event.run_id != scan_run_id:
                    raise TailerError(
                        f"event seq {event.seq} belongs to a different run"
                    )
                if event.seq != expected_seq:
                    raise TailerError(
                        f"event sequence gap: expected {expected_seq}, got {event.seq}"
                    )
                if event.prev_hash != expected_prev_hash:
                    raise TailerError(f"event seq {event.seq}: broken prev_hash chain")
                expected_seq += 1
                expected_prev_hash = event.hash

                if open_tick is None:
                    open_tick = _open_transaction(event, committed_boundary.last_tick)
                    continue

                _accept_transaction_event(open_tick, event)
                if event.type != "tick_commit":
                    continue

                committed_offset = line_end
                committed_boundary = _VerifiedBoundary(
                    run_id=scan_run_id,
                    next_seq=expected_seq,
                    prev_hash=expected_prev_hash,
                    last_tick=event.tick,
                )
                transaction = open_tick.events
                open_tick = None
                if collect:
                    for verified_event in transaction:
                        if (
                            verified_event.seq <= after_seq
                            or (
                                before_seq is not None
                                and verified_event.seq >= before_seq
                            )
                        ):
                            continue
                        projected = (
                            verified_event.to_dict()
                            if self.projector is None
                            else self.projector(verified_event.to_dict())
                        )
                        if projected is not None and (
                            reverse_page or limit is None or len(emitted) < limit
                        ):
                            emitted.append(projected)

                if required_anchor == (event.seq, event.hash):
                    anchor_found = True
                    if stop_at_anchor:
                        break
                if (
                    before_seq is not None
                    and event.seq >= before_seq
                ):
                    break
                if not reverse_page and limit is not None and len(emitted) >= limit:
                    break

        if required_anchor is not None and not anchor_found and not stop_at_anchor:
            raise TailerError("event log does not contain the requested commit anchor")
        return _VerifiedScan(
            committed_offset=committed_offset,
            boundary=committed_boundary,
            emitted=tuple(emitted),
            anchor_found=anchor_found,
        )

    def _apply_scan(
        self,
        scan: _VerifiedScan,
        *,
        identity: tuple[int, int] | None = None,
    ) -> None:
        self._committed_offset = scan.committed_offset
        self._boundary = scan.boundary
        if identity is not None:
            self._identity = identity


def _open_transaction(event: Event, last_tick: int | None) -> _OpenTransaction:
    if event.type != "tick_begin":
        raise TailerError(f"event seq {event.seq} is outside a tick transaction")
    expected_tick = None if last_tick is None else last_tick + 1
    if expected_tick is not None and event.tick != expected_tick:
        raise TailerError(f"tick sequence gap: expected {expected_tick}, got {event.tick}")
    if set(event.payload) != {"event_count"}:
        raise TailerError("tick_begin payload does not match schema")
    count = event.payload["event_count"]
    if type(count) is not int or count < 0:
        raise TailerError("tick_begin event_count must be non-negative")
    return _OpenTransaction(
        begin=event,
        expected_user_events=count,
        events=[event],
    )


def _accept_transaction_event(open_tick: _OpenTransaction, event: Event) -> None:
    if event.tick != open_tick.begin.tick:
        raise TailerError(f"event seq {event.seq} changed tick inside a transaction")
    open_tick.events.append(event)
    if event.type == "tick_begin":
        raise TailerError("nested tick_begin event")
    if event.type == "state_checkpoint":
        if open_tick.checkpoint is not None:
            raise TailerError("duplicate state_checkpoint event")
        if open_tick.user_events != open_tick.expected_user_events:
            raise TailerError("state_checkpoint appears before declared user events")
        _validate_checkpoint(event)
        open_tick.checkpoint = event
        return
    if event.type == "tick_commit":
        if open_tick.checkpoint is None:
            raise TailerError("tick_commit without state_checkpoint")
        _validate_commit(event, open_tick)
        return
    if event.type in RESERVED_EVENT_TYPES:
        raise TailerError(f"invalid reserved event ordering: {event.type}")
    if open_tick.checkpoint is not None:
        raise TailerError("user event appears after state_checkpoint")
    open_tick.user_events += 1
    if open_tick.user_events > open_tick.expected_user_events:
        raise TailerError("more user events than tick_begin declared")


def _validate_checkpoint(event: Event) -> None:
    if set(event.payload) != {"state", "rng_state", "state_hash"}:
        raise TailerError("state_checkpoint payload does not match schema")
    state = event.payload["state"]
    rng_state = event.payload["rng_state"]
    state_hash = event.payload["state_hash"]
    if not isinstance(state, dict) or not isinstance(rng_state, dict):
        raise TailerError("checkpoint state and rng_state must be JSON objects")
    if "rng_state" in state and state["rng_state"] != rng_state:
        raise TailerError("checkpoint rng_state does not match state.rng_state")
    if not isinstance(state_hash, str) or state_hash != json_sha256(state):
        raise TailerError("checkpoint state_hash mismatch")


def _validate_commit(event: Event, open_tick: _OpenTransaction) -> None:
    expected_keys = {
        "begin_seq",
        "event_count",
        "checkpoint_hash",
        "checkpoint_event_hash",
    }
    if set(event.payload) != expected_keys:
        raise TailerError("tick_commit payload does not match schema")
    checkpoint = open_tick.checkpoint
    assert checkpoint is not None
    expected = {
        "begin_seq": open_tick.begin.seq,
        "event_count": open_tick.expected_user_events,
        "checkpoint_hash": checkpoint.payload["state_hash"],
        "checkpoint_event_hash": checkpoint.hash,
    }
    if event.payload != expected:
        raise TailerError("tick_commit does not attest its transaction")


def read_committed_events(
    path: str | Path,
    *,
    after_seq: int = -1,
    before_seq: int | None = None,
    limit: int | None = None,
    projector: EventProjector | None = project_event,
    tail: bool = False,
    anchor: tuple[int, str] | None = None,
    require_anchor: bool = False,
) -> list[StreamItem]:
    """Read a bounded page from the committed log without opening a writer."""

    if type(after_seq) is not int or after_seq < -1:
        raise ValueError("after_seq must be an integer greater than or equal to -1")
    if before_seq is not None and (type(before_seq) is not int or before_seq < 0):
        raise ValueError("before_seq must be a non-negative integer or None")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer or None")
    if tail and limit is None:
        raise ValueError("tail reads require a finite limit")
    if before_seq is not None and limit is None:
        raise ValueError("reverse pages require a finite limit")
    if before_seq is not None and (after_seq != -1 or tail):
        raise ValueError("before_seq cannot be combined with after_seq or tail")
    reader = EventLogTailer(path, projector=projector)
    try:
        fd = os.open(reader.path, _READ_FLAGS)
    except FileNotFoundError:
        if anchor is not None and require_anchor:
            raise TailerError(
                "event log does not contain the requested commit anchor"
            ) from None
        return []
    except OSError as exc:
        raise TailerError("event log is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TailerError("event log must be a private regular file")
        identity = (info.st_dev, info.st_ino)
        if anchor is not None:
            seq, event_hash = anchor
            if (
                type(seq) is not int
                or seq < 0
                or not isinstance(event_hash, str)
                or HASH_RE.fullmatch(event_hash) is None
            ):
                raise ValueError("commit anchor must contain a sequence and hash")
            positioned = reader._scan_fd(
                fd,
                start_offset=0,
                boundary=_VerifiedBoundary(),
                collect=False,
                required_anchor=anchor,
                stop_at_anchor=True,
            )
            if not positioned.anchor_found:
                if require_anchor:
                    raise TailerError(
                        "event log does not contain the requested commit anchor"
                    )
                os.lseek(fd, 0, os.SEEK_SET)
            else:
                reader._apply_scan(positioned, identity=identity)
        return reader._read_fd(
            fd,
            after_seq=after_seq,
            before_seq=before_seq,
            limit=limit,
            tail=tail,
        )
    finally:
        os.close(fd)


def read_durable_head(path: str | Path) -> tuple[int | None, str | None]:
    """Read the authoritative JSONL head after verifying committed history."""

    tailer = EventLogTailer(path, projector=None)
    tailer.prime_to_latest_commit()
    return tailer.last_seq, tailer.last_hash


def _event_seq(event: Mapping[str, object]) -> int:
    seq = event.get("seq")
    return seq if type(seq) is int else -1


class EventBroadcaster:
    """Fan out stream items to bounded per-client queues.

    Slow clients receive a single ``gap`` marker and must use the REST event
    endpoint to backfill from their last SSE ID.  No unbounded history is kept in
    the GUI process.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        self.queue_size = queue_size
        self._subscribers: set[asyncio.Queue[StreamItem]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[StreamItem]:
        queue: asyncio.Queue[StreamItem] = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StreamItem]) -> None:
        self._subscribers.discard(queue)

    def publish_nowait(self, event: Mapping[str, object]) -> None:
        detached = dict(event)
        latest_seq = _event_seq(detached)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(detached)
            except asyncio.QueueFull:
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(
                    {
                        "kind": "gap",
                        "type": "gap",
                        "latest_seq": latest_seq,
                    }
                )

    async def publish(self, event: Mapping[str, object]) -> None:
        self.publish_nowait(event)

    async def publish_many(self, events: Iterable[Mapping[str, object]]) -> None:
        for event in events:
            self.publish_nowait(event)


class EventStreamHub:
    """Poll one run log and share future committed events across SSE clients."""

    def __init__(
        self,
        path: str | Path,
        *,
        poll_interval: float = 0.25,
        queue_size: int = 256,
        writer_active: WriterProbe | None = None,
        unsafe_batch_observer: UnsafeBatchObserver | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.tailer = EventLogTailer(path)
        self.broadcaster = EventBroadcaster(queue_size=queue_size)
        self.poll_interval = poll_interval
        self.writer_active = writer_active
        self.unsafe_batch_observer = unsafe_batch_observer
        self._task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._projection_gated = False

    async def subscribe(
        self,
        *,
        anchor: tuple[int, str] | None = None,
        prime_latest: bool = True,
        projection_gated: bool = False,
    ) -> asyncio.Queue[StreamItem]:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("event stream hub is closed")
            if self._task is None or self._task.done():
                # REST supplies historical backfill.  Prime the durable offset
                # before adding the first subscriber so the hub publishes only
                # commits made after that boundary.
                if anchor is not None:
                    self.tailer.position_after_commit(*anchor)
                elif prime_latest:
                    self.tailer.prime_to_latest_commit()
                self._task = asyncio.create_task(self._run(), name="terrarium-event-tailer")
            self._projection_gated = self._projection_gated or projection_gated
            return self.broadcaster.subscribe()

    def unsubscribe(self, queue: asyncio.Queue[StreamItem]) -> None:
        self.broadcaster.unsubscribe(queue)

    async def close(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            while not self._closed:
                active_before = self._probe_writer()
                try:
                    events = self.tailer.poll()
                except TailerError:
                    self.broadcaster.publish_nowait(
                        {"kind": "gap", "type": "gap", "latest_seq": self.tailer.last_seq or -1}
                    )
                else:
                    active_after = self._probe_writer()
                    if (
                        events
                        and self.unsafe_batch_observer is not None
                        and (self._projection_gated or active_before or active_after)
                    ):
                        # Latch the batch before it reaches any subscriber.  In
                        # particular this closes the race where the writer
                        # releases its lock between poll() and SSE delivery.
                        self.unsafe_batch_observer(events)
                    await self.broadcaster.publish_many(events)
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise

    def _probe_writer(self) -> bool:
        if self.writer_active is None:
            return False
        try:
            return bool(self.writer_active())
        except OSError:
            return True


# Descriptive alias used by a few integration call sites.
BoundedBroadcaster = EventBroadcaster


__all__ = [
    "BoundedBroadcaster",
    "EventBroadcaster",
    "EventLogTailer",
    "EventStreamHub",
    "StreamItem",
    "TailerError",
    "read_committed_events",
    "read_durable_head",
]
