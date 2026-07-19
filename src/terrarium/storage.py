"""Crash-safe storage for Terrarium runs.

Durability has a deliberately simple authority order:

1. ``events.jsonl`` is append-only and authoritative.
2. A tick exists only after its ``tick_commit`` event has been flushed and
   ``fsync``-ed.
3. SQLite is an idempotent projection and can always be rebuilt from JSONL.

Model output is untrusted.  It is never interpolated into SQL or filesystem
paths, and raw responses are written as immutable, private files beneath one
fixed run root.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import secrets
import sqlite3
import stat
import threading
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .events import (
    GENESIS_HASH,
    SCHEMA_VERSION,
    Event,
    EventValidationError,
    canonical_json,
    canonical_json_bytes,
    json_sha256,
    strict_json_loads,
    validate_event_type,
    validate_run_id,
)

EVENT_LOG_NAME: Final = "events.jsonl"
SQLITE_NAME: Final = "state.sqlite3"
RAW_DIRECTORY_NAME: Final = "raw"
RECOVERY_DIRECTORY_NAME: Final = "recovery"
LOCK_NAME: Final = ".writer.lock"
DEFAULT_MAX_EVENT_LINE_BYTES: Final = 16 * 1024 * 1024
DEFAULT_MAX_RAW_BYTES: Final = 16 * 1024 * 1024
RESERVED_EVENT_TYPES: Final = frozenset({"tick_begin", "state_checkpoint", "tick_commit"})


class StorageError(RuntimeError):
    """Base class for persistence failures."""


class StorageIntegrityError(StorageError):
    """Durable state is corrupt, inconsistent, or unexpectedly replaced."""


class StoreLockedError(StorageError):
    """Another writer currently owns this run directory."""


class ProjectionError(StorageError):
    """JSONL is durable, but its replaceable SQLite projection failed."""


class RawResponseError(StorageError):
    """A raw response could not be stored safely."""


@dataclass(slots=True)
class _OpenTick:
    tick: int
    begin_offset: int
    expected_user_events: int
    events: list[Event] = field(default_factory=list)
    user_events: int = 0
    checkpoint: Event | None = None


@dataclass(frozen=True, slots=True)
class _ScanResult:
    transactions: tuple[tuple[Event, ...], ...]
    events: tuple[Event, ...]
    committed_end: int
    file_size: int
    incomplete_tail_bytes: int
    last_tick: int | None
    last_seq: int | None
    last_hash: str


_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK (json_valid(value_json))
) STRICT;

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY CHECK (seq >= 0),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
    tick INTEGER NOT NULL CHECK (tick >= 0),
    type TEXT NOT NULL CHECK (length(type) BETWEEN 1 AND 128),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    prev_hash TEXT NOT NULL CHECK (length(prev_hash) = 64),
    hash TEXT NOT NULL UNIQUE CHECK (length(hash) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS events_tick_idx ON events(tick, seq);
CREATE INDEX IF NOT EXISTS events_type_idx ON events(type, tick);

CREATE TABLE IF NOT EXISTS checkpoints (
    tick INTEGER PRIMARY KEY CHECK (tick >= 0),
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq) ON DELETE RESTRICT,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    rng_state_json TEXT NOT NULL CHECK (json_valid(rng_state_json)),
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS generations (
    generation_id TEXT PRIMARY KEY,
    started_tick INTEGER CHECK (started_tick IS NULL OR started_tick >= 0),
    ended_tick INTEGER CHECK (ended_tick IS NULL OR ended_tick >= 0),
    valley TEXT,
    model TEXT,
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq) ON DELETE RESTRICT,
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    generation_id TEXT,
    model TEXT,
    valley TEXT,
    born_tick INTEGER CHECK (born_tick IS NULL OR born_tick >= 0),
    died_tick INTEGER CHECK (died_tick IS NULL OR died_tick >= 0),
    created_event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq) ON DELETE RESTRICT,
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE TABLE IF NOT EXISTS legacies (
    legacy_id TEXT PRIMARY KEY,
    author_agent_id TEXT,
    generation_id TEXT,
    valley TEXT,
    channel TEXT NOT NULL,
    text TEXT NOT NULL,
    parent_legacy_ids_json TEXT NOT NULL CHECK (json_valid(parent_legacy_ids_json)),
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events(seq) ON DELETE RESTRICT,
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE TRIGGER IF NOT EXISTS legacies_immutable_update
BEFORE UPDATE ON legacies
BEGIN
    SELECT RAISE(ABORT, 'legacy records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS legacies_immutable_delete
BEFORE DELETE ON legacies
BEGIN
    SELECT RAISE(ABORT, 'legacy records are immutable');
END;

CREATE TABLE IF NOT EXISTS actions (
    event_seq INTEGER PRIMARY KEY REFERENCES events(seq) ON DELETE RESTRICT,
    action_id TEXT UNIQUE,
    tick INTEGER NOT NULL CHECK (tick >= 0),
    agent_id TEXT,
    action_type TEXT,
    valid INTEGER NOT NULL CHECK (valid IN (0, 1)),
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE INDEX IF NOT EXISTS actions_agent_tick_idx ON actions(agent_id, tick);

CREATE TABLE IF NOT EXISTS deaths (
    event_seq INTEGER PRIMARY KEY REFERENCES events(seq) ON DELETE RESTRICT,
    tick INTEGER NOT NULL CHECK (tick >= 0),
    agent_id TEXT,
    cause TEXT,
    generation_id TEXT,
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE INDEX IF NOT EXISTS deaths_agent_idx ON deaths(agent_id, tick);

CREATE TABLE IF NOT EXISTS surveys (
    event_seq INTEGER PRIMARY KEY REFERENCES events(seq) ON DELETE RESTRICT,
    survey_id TEXT NOT NULL,
    tick INTEGER NOT NULL CHECK (tick >= 0),
    agent_id TEXT,
    phase TEXT,
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE INDEX IF NOT EXISTS surveys_agent_idx ON surveys(agent_id, tick);

CREATE TABLE IF NOT EXISTS token_usage (
    event_seq INTEGER PRIMARY KEY REFERENCES events(seq) ON DELETE RESTRICT,
    tick INTEGER NOT NULL CHECK (tick >= 0),
    agent_id TEXT,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL CHECK (reasoning_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    data_json TEXT NOT NULL CHECK (json_valid(data_json))
) STRICT;

CREATE INDEX IF NOT EXISTS token_usage_model_idx ON token_usage(provider, model, tick);
"""

_PROJECTION_TABLES = (
    "meta",
    "events",
    "checkpoints",
    "generations",
    "agents",
    "legacies",
    "actions",
    "deaths",
    "surveys",
    "token_usage",
)


def _os_flag(name: str) -> int:
    return int(getattr(os, name, 0))


_SAFE_OPEN_FLAGS = _os_flag("O_CLOEXEC") | _os_flag("O_NOFOLLOW")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting durable data")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | _os_flag("O_DIRECTORY") | _os_flag("O_CLOEXEC")
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path, *, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StorageIntegrityError(f"not a real directory: {path}")
    return path.resolve(strict=True)


def _secure_regular_file(path: Path, *, read_write: bool = True) -> int:
    access = os.O_RDWR if read_write else os.O_RDONLY
    flags = access | os.O_CREAT | _SAFE_OPEN_FLAGS
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StorageIntegrityError(f"not a regular file: {path}")
        if info.st_nlink != 1:
            raise StorageIntegrityError(f"refusing multiply-linked durable file: {path}")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _prepare_sqlite_file(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StorageIntegrityError(f"unsafe SQLite path: {path}")
        if info.st_nlink != 1:
            raise StorageIntegrityError(f"refusing multiply-linked SQLite file: {path}")
        os.chmod(path, 0o600)
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _SAFE_OPEN_FLAGS, 0o600)
    os.close(fd)
    _fsync_directory(path.parent)


def _safe_unlink(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StorageIntegrityError(f"refusing to unlink unsafe path: {path}")
    path.unlink()


def _json_object(value: Mapping[str, Any], *, name: str) -> dict[str, Any]:
    try:
        detached = strict_json_loads(canonical_json(dict(value)))
    except EventValidationError as exc:
        raise StorageError(f"{name} is not a finite JSON object: {exc}") from exc
    if not isinstance(detached, dict):
        raise StorageError(f"{name} must be a JSON object")
    return detached


def _id(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if type(value) is int:
            return str(value)
    return None


def _text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _nonnegative_int(payload: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if type(value) is int and value >= 0:
            return value
    return 0


def _open_sqlite(path: Path, *, wal: bool) -> sqlite3.Connection:
    _prepare_sqlite_file(path)
    connection = sqlite3.connect(
        str(path),
        timeout=5.0,
        isolation_level=None,
        check_same_thread=True,
    )
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        if wal:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise StorageIntegrityError("SQLite refused WAL mode")
        else:
            connection.execute("PRAGMA journal_mode = DELETE")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()
        if enabled is None or enabled[0] != 1:
            raise StorageIntegrityError("SQLite foreign keys are not enabled")
        return connection
    except BaseException:
        connection.close()
        raise


class EventStore:
    """Single-writer, crash-recoverable persistence for one run.

    ``run_dir`` is the exact fixed root for the run; ``run_id`` is logical
    metadata and is never interpreted as a path.
    """

    def __init__(
        self,
        run_dir: str | os.PathLike[str],
        run_id: str,
        *,
        max_event_line_bytes: int = DEFAULT_MAX_EVENT_LINE_BYTES,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
    ) -> None:
        validate_run_id(run_id)
        if type(max_event_line_bytes) is not int or max_event_line_bytes < 1024:
            raise ValueError("max_event_line_bytes must be an integer >= 1024")
        if type(max_raw_bytes) is not int or max_raw_bytes < 1:
            raise ValueError("max_raw_bytes must be a positive integer")

        candidate = Path(run_dir).absolute()
        self.run_dir = _ensure_directory(candidate)
        self.run_id = run_id
        self.max_event_line_bytes = max_event_line_bytes
        self.max_raw_bytes = max_raw_bytes
        self.event_log_path = self.run_dir / EVENT_LOG_NAME
        self.sqlite_path = self.run_dir / SQLITE_NAME
        self.raw_dir = self.run_dir / RAW_DIRECTORY_NAME
        self._mutex = threading.RLock()
        self._closed = False
        self._poisoned = False
        self._lock_fd: int | None = None
        self._event_fd: int | None = None
        self._event_identity: tuple[int, int] | None = None
        self._conn: sqlite3.Connection | None = None
        self._last_seq: int | None = None
        self._last_hash = GENESIS_HASH
        self._last_tick: int | None = None

        try:
            self._acquire_writer_lock()
            log_fd = _secure_regular_file(self.event_log_path)
            os.close(log_fd)
            _fsync_directory(self.run_dir)
            scan = self._scan_log()
            if scan.incomplete_tail_bytes:
                self._truncate_incomplete_tail(scan.committed_end)
                scan = self._scan_log()
            self._event_fd = os.open(
                self.event_log_path,
                os.O_WRONLY | os.O_APPEND | _SAFE_OPEN_FLAGS,
            )
            info = os.fstat(self._event_fd)
            self._event_identity = (info.st_dev, info.st_ino)

            self._conn = _open_sqlite(self.sqlite_path, wal=True)
            self._initialize_schema(self._conn)
            self._bind_database_to_run(self._conn)
            self._validate_database_not_ahead(self._conn, scan)
            self._project_transactions(self._conn, scan.transactions)
            # Never expose a pre-existing projection merely because it is not ahead.
            # Reproject JSONL independently and compare every table before resume.
            self._verify_sqlite(scan)

            self._last_seq = scan.last_seq
            self._last_hash = scan.last_hash
            self._last_tick = scan.last_tick
        except BaseException:
            self.close()
            raise

    def _acquire_writer_lock(self) -> None:
        lock_path = self.run_dir / LOCK_NAME
        fd = _secure_regular_file(lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise StoreLockedError(f"run already has a writer: {self.run_dir}") from exc
        self._lock_fd = fd

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(_SQL_SCHEMA)
        except sqlite3.DatabaseError as exc:
            raise StorageIntegrityError(f"cannot initialize SQLite schema: {exc}") from exc

    def _bind_database_to_run(self, connection: sqlite3.Connection) -> None:
        existing_run = self._get_meta(connection, "run_id")
        if existing_run is None:
            self._set_meta(connection, "run_id", self.run_id)
            self._set_meta(connection, "schema_version", SCHEMA_VERSION)
        elif existing_run != self.run_id:
            raise StorageIntegrityError(
                f"SQLite belongs to run {existing_run!r}, not {self.run_id!r}"
            )
        existing_version = self._get_meta(connection, "schema_version")
        if existing_version != SCHEMA_VERSION:
            raise StorageIntegrityError(f"unsupported SQLite schema version: {existing_version!r}")

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            """
            INSERT INTO meta(key, value_json) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """,
            (key, canonical_json(value)),
        )

    @staticmethod
    def _get_meta(connection: sqlite3.Connection, key: str) -> Any:
        row = connection.execute("SELECT value_json FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return strict_json_loads(row[0])
        except EventValidationError as exc:
            raise StorageIntegrityError(f"invalid meta value for {key!r}: {exc}") from exc

    def _scan_log(self) -> _ScanResult:
        fd = os.open(self.event_log_path, os.O_RDONLY | _SAFE_OPEN_FLAGS)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageIntegrityError("event log is not a private regular file")
            file_size = info.st_size
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return self._scan_stream(stream, file_size)
        finally:
            os.close(fd)

    def _scan_stream(self, stream: Any, file_size: int) -> _ScanResult:
        transactions: list[tuple[Event, ...]] = []
        committed_events: list[Event] = []
        committed_end = 0
        expected_seq = 0
        expected_prev = GENESIS_HASH
        latest_tick: int | None = None
        open_tick: _OpenTick | None = None

        while True:
            line_start = stream.tell()
            line = stream.readline(self.max_event_line_bytes + 1)
            if not line:
                break
            if len(line) > self.max_event_line_bytes:
                raise StorageIntegrityError(
                    f"event log line at byte {line_start} exceeds configured limit"
                )
            # A physical record is complete only with its newline.  A crash can
            # leave a complete-looking JSON object without that final byte.
            if not line.endswith(b"\n"):
                tail_start = open_tick.begin_offset if open_tick else committed_end
                return self._scan_result(
                    transactions,
                    committed_events,
                    committed_end,
                    file_size,
                    file_size - tail_start,
                    latest_tick,
                )
            try:
                parsed = strict_json_loads(line[:-1])
                if not isinstance(parsed, dict):
                    raise EventValidationError("event line must contain a JSON object")
                event = Event.from_dict(parsed)
            except EventValidationError as exc:
                raise StorageIntegrityError(
                    f"invalid event record at byte {line_start}: {exc}"
                ) from exc

            if event.run_id != self.run_id:
                raise StorageIntegrityError(
                    f"event seq {event.seq} belongs to run {event.run_id!r}"
                )
            if event.seq != expected_seq:
                raise StorageIntegrityError(
                    f"event sequence gap: expected {expected_seq}, got {event.seq}"
                )
            if event.prev_hash != expected_prev:
                raise StorageIntegrityError(f"event seq {event.seq}: broken prev_hash chain")
            expected_seq += 1
            expected_prev = event.hash

            if open_tick is None:
                if event.type != "tick_begin":
                    raise StorageIntegrityError(
                        f"event seq {event.seq} is outside a tick transaction"
                    )
                expected_tick = None if latest_tick is None else latest_tick + 1
                if expected_tick is not None and event.tick != expected_tick:
                    raise StorageIntegrityError(
                        f"tick sequence gap: expected {expected_tick}, got {event.tick}"
                    )
                if set(event.payload) != {"event_count"}:
                    raise StorageIntegrityError("tick_begin payload does not match schema")
                count = event.payload["event_count"]
                if type(count) is not int or count < 0:
                    raise StorageIntegrityError("tick_begin event_count must be non-negative")
                open_tick = _OpenTick(event.tick, line_start, count, events=[event])
                continue

            if event.tick != open_tick.tick:
                raise StorageIntegrityError(
                    f"event seq {event.seq} changed tick inside a transaction"
                )
            open_tick.events.append(event)

            if event.type == "tick_begin":
                raise StorageIntegrityError("nested tick_begin event")
            if event.type == "state_checkpoint":
                if open_tick.checkpoint is not None:
                    raise StorageIntegrityError("duplicate state_checkpoint event")
                if open_tick.user_events != open_tick.expected_user_events:
                    raise StorageIntegrityError(
                        "state_checkpoint appears before declared user events"
                    )
                self._validate_checkpoint_event(event)
                open_tick.checkpoint = event
                continue
            if event.type == "tick_commit":
                if open_tick.checkpoint is None:
                    raise StorageIntegrityError("tick_commit without state_checkpoint")
                self._validate_commit_event(event, open_tick)
                transaction = tuple(open_tick.events)
                transactions.append(transaction)
                committed_events.extend(transaction)
                committed_end = stream.tell()
                latest_tick = open_tick.tick
                open_tick = None
                continue
            if event.type in RESERVED_EVENT_TYPES:
                raise StorageIntegrityError(f"invalid reserved event ordering: {event.type}")
            if open_tick.checkpoint is not None:
                raise StorageIntegrityError("user event appears after state_checkpoint")
            open_tick.user_events += 1
            if open_tick.user_events > open_tick.expected_user_events:
                raise StorageIntegrityError("more user events than tick_begin declared")

        incomplete = file_size - committed_end if open_tick is not None else 0
        return self._scan_result(
            transactions,
            committed_events,
            committed_end,
            file_size,
            incomplete,
            latest_tick,
        )

    @staticmethod
    def _scan_result(
        transactions: Sequence[tuple[Event, ...]],
        events: Sequence[Event],
        committed_end: int,
        file_size: int,
        incomplete: int,
        last_tick: int | None,
    ) -> _ScanResult:
        last = events[-1] if events else None
        return _ScanResult(
            transactions=tuple(transactions),
            events=tuple(events),
            committed_end=committed_end,
            file_size=file_size,
            incomplete_tail_bytes=incomplete,
            last_tick=last_tick,
            last_seq=None if last is None else last.seq,
            last_hash=GENESIS_HASH if last is None else last.hash,
        )

    @staticmethod
    def _validate_checkpoint_event(event: Event) -> None:
        if set(event.payload) != {"state", "rng_state", "state_hash"}:
            raise StorageIntegrityError("state_checkpoint payload does not match schema")
        state = event.payload["state"]
        rng_state = event.payload["rng_state"]
        state_hash = event.payload["state_hash"]
        if not isinstance(state, dict) or not isinstance(rng_state, dict):
            raise StorageIntegrityError("checkpoint state and rng_state must be JSON objects")
        if "rng_state" in state and state["rng_state"] != rng_state:
            raise StorageIntegrityError("checkpoint rng_state does not match state.rng_state")
        if not isinstance(state_hash, str) or state_hash != json_sha256(state):
            raise StorageIntegrityError("checkpoint state_hash mismatch")

    @staticmethod
    def _validate_commit_event(event: Event, open_tick: _OpenTick) -> None:
        expected_keys = {
            "begin_seq",
            "event_count",
            "checkpoint_hash",
            "checkpoint_event_hash",
        }
        if set(event.payload) != expected_keys:
            raise StorageIntegrityError("tick_commit payload does not match schema")
        checkpoint = open_tick.checkpoint
        assert checkpoint is not None
        expected = {
            "begin_seq": open_tick.events[0].seq,
            "event_count": open_tick.expected_user_events,
            "checkpoint_hash": checkpoint.payload["state_hash"],
            "checkpoint_event_hash": checkpoint.hash,
        }
        if event.payload != expected:
            raise StorageIntegrityError("tick_commit does not attest its transaction")

    def _truncate_incomplete_tail(self, committed_end: int) -> None:
        # Logical append-only semantics apply to committed records.  Crash bytes
        # after the last commit must be removed before appending again, but are
        # first retained verbatim as a private forensic artifact.
        self._preserve_incomplete_tail(committed_end)
        fd = os.open(self.event_log_path, os.O_RDWR | _SAFE_OPEN_FLAGS)
        try:
            os.ftruncate(fd, committed_end)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.run_dir)

    def _preserve_incomplete_tail(self, committed_end: int) -> Path | None:
        source_fd = os.open(self.event_log_path, os.O_RDONLY | _SAFE_OPEN_FLAGS)
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
                raise StorageIntegrityError("unsafe event log during tail recovery")
            if committed_end >= source_info.st_size:
                return None
            recovery_dir = _ensure_directory(self.run_dir / RECOVERY_DIRECTORY_NAME)
            dir_flags = (
                os.O_RDONLY
                | _os_flag("O_DIRECTORY")
                | _os_flag("O_CLOEXEC")
                | _os_flag("O_NOFOLLOW")
            )
            directory_fd = os.open(recovery_dir, dir_flags)
            try:
                os.lseek(source_fd, committed_end, os.SEEK_SET)
                temporary = f".tmp-{secrets.token_hex(16)}"
                temp_created = False
                output_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _SAFE_OPEN_FLAGS,
                    0o600,
                    dir_fd=directory_fd,
                )
                temp_created = True
                digest = hashlib.sha256()
                try:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        _write_all(output_fd, chunk)
                    os.fsync(output_fd)
                    os.fchmod(output_fd, 0o600)
                finally:
                    os.close(output_fd)
                target = f"incomplete-tail-{committed_end}-{digest.hexdigest()}.bin"
                try:
                    os.link(
                        temporary,
                        target,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # Content-addressing makes this an idempotent recovery of
                    # the same bytes, not permission to replace an artifact.
                    existing_fd = os.open(
                        target,
                        os.O_RDONLY | _SAFE_OPEN_FLAGS,
                        dir_fd=directory_fd,
                    )
                    try:
                        existing_info = os.fstat(existing_fd)
                        existing_digest = hashlib.sha256()
                        while True:
                            chunk = os.read(existing_fd, 1024 * 1024)
                            if not chunk:
                                break
                            existing_digest.update(chunk)
                        if (
                            not stat.S_ISREG(existing_info.st_mode)
                            or existing_digest.digest() != digest.digest()
                        ):
                            raise StorageIntegrityError(
                                "existing recovery artifact conflicts with crash tail"
                            )
                    finally:
                        os.close(existing_fd)
                os.unlink(temporary, dir_fd=directory_fd)
                temp_created = False
                os.fsync(directory_fd)
                return recovery_dir / target
            finally:
                if temp_created:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                os.close(directory_fd)
        finally:
            os.close(source_fd)

    def _assert_event_file_identity(self) -> None:
        if self._event_fd is None or self._event_identity is None:
            raise StorageError("event log is closed")
        fd_info = os.fstat(self._event_fd)
        path_info = self.event_log_path.lstat()
        identity = (path_info.st_dev, path_info.st_ino)
        if stat.S_ISLNK(path_info.st_mode) or identity != self._event_identity:
            raise StorageIntegrityError("event log was replaced while the store was open")
        if (fd_info.st_dev, fd_info.st_ino) != self._event_identity:
            raise StorageIntegrityError("event log descriptor identity changed")

    def _validate_database_not_ahead(
        self, connection: sqlite3.Connection, scan: _ScanResult
    ) -> None:
        row = connection.execute("SELECT MAX(seq), COUNT(*) FROM events").fetchone()
        assert row is not None
        max_seq, count = row
        committed_count = len(scan.events)
        if max_seq is not None and (scan.last_seq is None or max_seq > scan.last_seq):
            raise StorageIntegrityError("SQLite projection is ahead of authoritative JSONL")
        if count > committed_count:
            raise StorageIntegrityError("SQLite contains events absent from JSONL")
        projected = self._get_meta(connection, "projected_seq")
        if projected is not None and (
            type(projected) is not int or scan.last_seq is None or projected > scan.last_seq
        ):
            raise StorageIntegrityError("SQLite projected_seq is ahead of JSONL")

    def _project_transactions(
        self,
        connection: sqlite3.Connection,
        transactions: Iterable[Sequence[Event]],
    ) -> None:
        for transaction in transactions:
            self._project_tick(connection, transaction)

    def _project_tick(self, connection: sqlite3.Connection, transaction: Sequence[Event]) -> None:
        if not transaction or transaction[-1].type != "tick_commit":
            raise StorageIntegrityError("projector received an uncommitted transaction")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for event in transaction:
                self._project_event(connection, event)
            last = transaction[-1]
            self._set_meta(connection, "projected_seq", last.seq)
            self._set_meta(connection, "projected_hash", last.hash)
            self._set_meta(connection, "latest_tick", last.tick)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _project_event(self, connection: sqlite3.Connection, event: Event) -> None:
        payload_json = canonical_json(event.payload)
        connection.execute(
            """
            INSERT INTO events(
                seq, schema_version, run_id, tick, type, payload_json, prev_hash, hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(seq) DO NOTHING
            """,
            (
                event.seq,
                event.schema_version,
                event.run_id,
                event.tick,
                event.type,
                payload_json,
                event.prev_hash,
                event.hash,
            ),
        )
        row = connection.execute(
            "SELECT run_id, tick, type, payload_json, prev_hash, hash FROM events WHERE seq = ?",
            (event.seq,),
        ).fetchone()
        expected = (
            event.run_id,
            event.tick,
            event.type,
            payload_json,
            event.prev_hash,
            event.hash,
        )
        if row != expected:
            raise StorageIntegrityError(
                f"SQLite event seq {event.seq} conflicts with authoritative JSONL"
            )

        if event.type == "state_checkpoint":
            connection.execute(
                """
                INSERT INTO checkpoints(
                    tick, event_seq, state_json, rng_state_json, state_hash
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tick) DO NOTHING
                """,
                (
                    event.tick,
                    event.seq,
                    canonical_json(event.payload["state"]),
                    canonical_json(event.payload["rng_state"]),
                    event.payload["state_hash"],
                ),
            )
            row = connection.execute(
                "SELECT event_seq, state_hash FROM checkpoints WHERE tick = ?",
                (event.tick,),
            ).fetchone()
            if row != (event.seq, event.payload["state_hash"]):
                raise StorageIntegrityError(
                    f"SQLite checkpoint tick {event.tick} conflicts with JSONL"
                )

        self._project_domain_event(connection, event, payload_json)

    def _project_domain_event(
        self, connection: sqlite3.Connection, event: Event, payload_json: str
    ) -> None:
        payload = event.payload
        event_type = event.type

        if event_type in {"generation", "generation_started", "generation_spawned"}:
            generation_id = _id(payload, "generation_id", "generation", "id")
            if generation_id is not None:
                connection.execute(
                    """
                    INSERT INTO generations(
                        generation_id, started_tick, ended_tick, valley, model,
                        event_seq, data_json
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                    ON CONFLICT(generation_id) DO NOTHING
                    """,
                    (
                        generation_id,
                        event.tick,
                        _text(payload, "valley"),
                        _text(payload, "model"),
                        event.seq,
                        payload_json,
                    ),
                )
        elif event_type in {"generation_ended", "generation_completed"}:
            generation_id = _id(payload, "generation_id", "generation", "id")
            if generation_id is not None:
                connection.execute(
                    "UPDATE generations SET ended_tick = ? WHERE generation_id = ?",
                    (event.tick, generation_id),
                )

        if event_type in {"agent", "agent_spawned", "agent_created", "spawn"}:
            agent_id = _id(payload, "agent_id", "agent", "id")
            if agent_id is not None:
                connection.execute(
                    """
                    INSERT INTO agents(
                        agent_id, generation_id, model, valley, born_tick, died_tick,
                        created_event_seq, data_json
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(agent_id) DO NOTHING
                    """,
                    (
                        agent_id,
                        _id(payload, "generation_id", "generation"),
                        _text(payload, "model"),
                        _text(payload, "valley"),
                        event.tick,
                        event.seq,
                        payload_json,
                    ),
                )

        if event_type in {"legacy", "legacy_created", "legacy_written", "oral_legacy"}:
            self._project_legacy(connection, event, payload_json)

        if event_type in {
            "action",
            "intent",
            "action_resolved",
            "invalid_action",
            "action_validated",
        }:
            nested_action = payload.get("action")
            action_type = _text(payload, "action_type", "kind", "type")
            if action_type is None and isinstance(nested_action, dict):
                action_type = _text(nested_action, "type", "kind")
            valid_value = payload.get("valid")
            valid = 0 if event_type == "invalid_action" or valid_value is False else 1
            connection.execute(
                """
                INSERT INTO actions(
                    event_seq, action_id, tick, agent_id, action_type, valid, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_seq) DO NOTHING
                """,
                (
                    event.seq,
                    _id(payload, "action_id", "intent_id"),
                    event.tick,
                    _id(payload, "agent_id", "agent"),
                    action_type,
                    valid,
                    payload_json,
                ),
            )

        if event_type in {"death", "agent_died", "death_recorded"}:
            agent_id = _id(payload, "agent_id", "agent", "id")
            connection.execute(
                """
                INSERT INTO deaths(
                    event_seq, tick, agent_id, cause, generation_id, data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_seq) DO NOTHING
                """,
                (
                    event.seq,
                    event.tick,
                    agent_id,
                    _text(payload, "cause", "reason"),
                    _id(payload, "generation_id", "generation"),
                    payload_json,
                ),
            )
            if agent_id is not None:
                connection.execute(
                    "UPDATE agents SET died_tick = ? WHERE agent_id = ?",
                    (event.tick, agent_id),
                )

        if event_type in {"survey", "survey_answered", "survey_response"}:
            response = payload.get("response", payload.get("answers", {}))
            # response_json may contain any canonical JSON value, not only objects.
            response_json = canonical_json(response)
            connection.execute(
                """
                INSERT INTO surveys(
                    event_seq, survey_id, tick, agent_id, phase, response_json, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_seq) DO NOTHING
                """,
                (
                    event.seq,
                    _id(payload, "survey_id", "probe_id", "id") or f"seq:{event.seq}",
                    event.tick,
                    _id(payload, "agent_id", "agent"),
                    _text(payload, "phase"),
                    response_json,
                    payload_json,
                ),
            )

        if event_type in {"token_usage", "usage", "model_usage"}:
            input_tokens = _nonnegative_int(payload, "input_tokens", "prompt_tokens")
            output_tokens = _nonnegative_int(payload, "output_tokens", "completion_tokens")
            reasoning_tokens = _nonnegative_int(payload, "reasoning_tokens")
            total_tokens = _nonnegative_int(payload, "total_tokens")
            if total_tokens == 0:
                total_tokens = input_tokens + output_tokens + reasoning_tokens
            connection.execute(
                """
                INSERT INTO token_usage(
                    event_seq, tick, agent_id, provider, model, input_tokens,
                    output_tokens, reasoning_tokens, total_tokens, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_seq) DO NOTHING
                """,
                (
                    event.seq,
                    event.tick,
                    _id(payload, "agent_id", "agent"),
                    _text(payload, "provider"),
                    _text(payload, "model"),
                    input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    total_tokens,
                    payload_json,
                ),
            )

    @staticmethod
    def _project_legacy(connection: sqlite3.Connection, event: Event, payload_json: str) -> None:
        payload = event.payload
        legacy_id = _id(payload, "legacy_id", "id")
        if legacy_id is None:
            return
        parent_ids = payload.get("parent_legacy_ids", [])
        if not isinstance(parent_ids, list) or any(
            not isinstance(parent, (str, int)) or isinstance(parent, bool) for parent in parent_ids
        ):
            raise StorageIntegrityError("legacy parent_legacy_ids must be an ID list")
        text = _text(payload, "text", "content")
        if text is None:
            raise StorageIntegrityError("legacy text must be a string")
        channel = _text(payload, "channel") or (
            "oral" if event.type == "oral_legacy" else "written"
        )
        connection.execute(
            """
            INSERT INTO legacies(
                legacy_id, author_agent_id, generation_id, valley, channel, text,
                parent_legacy_ids_json, event_seq, data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(legacy_id) DO NOTHING
            """,
            (
                legacy_id,
                _id(payload, "author_agent_id", "author", "agent_id"),
                _id(payload, "generation_id", "generation"),
                _text(payload, "valley"),
                channel,
                text,
                canonical_json(parent_ids),
                event.seq,
                payload_json,
            ),
        )
        row = connection.execute(
            "SELECT event_seq, data_json FROM legacies WHERE legacy_id = ?",
            (legacy_id,),
        ).fetchone()
        if row != (event.seq, payload_json):
            raise StorageIntegrityError(f"legacy {legacy_id!r} was redefined")

    def _ensure_usable(self) -> None:
        if self._closed:
            raise StorageError("EventStore is closed")
        if self._poisoned:
            raise StorageError("EventStore requires reopen/recovery after a failed commit")

    def commit_tick(
        self,
        tick: int,
        events: Iterable[Mapping[str, Any]],
        state: Mapping[str, Any],
    ) -> tuple[Event, ...]:
        """Durably append and project one complete tick.

        User events are mappings with exactly ``{"type", "payload"}``.  The
        store assigns all envelope fields.  ``state`` is an opaque JSON object;
        a top-level ``rng_state`` value, when present, must itself be an object.
        """

        with self._mutex:
            self._ensure_usable()
            if type(tick) is not int or tick < 0:
                raise StorageError("tick must be a non-negative integer")
            expected_tick = None if self._last_tick is None else self._last_tick + 1
            if expected_tick is not None and tick != expected_tick:
                raise StorageError(f"expected tick {expected_tick}, got {tick}")
            if not isinstance(state, Mapping):
                raise StorageError("state must be a JSON object")
            state_object = _json_object(state, name="state")
            rng_state = state_object.get("rng_state", {})
            if not isinstance(rng_state, dict):
                raise StorageError("state.rng_state must be a JSON object when present")

            user_events: list[tuple[str, dict[str, Any]]] = []
            for index, draft in enumerate(events):
                if not isinstance(draft, Mapping) or set(draft) != {"type", "payload"}:
                    raise StorageError(f"events[{index}] must have exactly 'type' and 'payload'")
                try:
                    event_type = validate_event_type(draft["type"])
                except EventValidationError as exc:
                    raise StorageError(f"events[{index}]: {exc}") from exc
                if event_type in RESERVED_EVENT_TYPES:
                    raise StorageError(f"events[{index}] uses reserved type {event_type!r}")
                payload = draft["payload"]
                if not isinstance(payload, Mapping):
                    raise StorageError(f"events[{index}].payload must be a JSON object")
                user_events.append(
                    (
                        event_type,
                        _json_object(payload, name=f"events[{index}].payload"),
                    )
                )

            transaction = self._build_transaction(
                tick=tick,
                user_events=user_events,
                state=state_object,
                rng_state=rng_state,
            )
            blob = b"".join(canonical_json_bytes(event.to_dict()) + b"\n" for event in transaction)
            for event in transaction:
                line_size = len(canonical_json_bytes(event.to_dict())) + 1
                if line_size > self.max_event_line_bytes:
                    raise StorageError(f"event seq {event.seq} exceeds max_event_line_bytes")

            try:
                self._assert_event_file_identity()
                assert self._event_fd is not None
                _write_all(self._event_fd, blob)
                os.fsync(self._event_fd)
            except BaseException:
                self._poisoned = True
                raise

            # JSONL is now authoritative, even if the replaceable projection
            # below fails.  Update chain state before surfacing that failure.
            last = transaction[-1]
            self._last_seq = last.seq
            self._last_hash = last.hash
            self._last_tick = last.tick
            try:
                assert self._conn is not None
                self._project_tick(self._conn, transaction)
            except BaseException as exc:
                self._poisoned = True
                raise ProjectionError(
                    "tick is durable in JSONL, but SQLite projection failed; reopen store"
                ) from exc
            return transaction

    def _build_transaction(
        self,
        *,
        tick: int,
        user_events: Sequence[tuple[str, dict[str, Any]]],
        state: dict[str, Any],
        rng_state: dict[str, Any],
    ) -> tuple[Event, ...]:
        next_seq = 0 if self._last_seq is None else self._last_seq + 1
        prev_hash = self._last_hash
        result: list[Event] = []

        def add(event_type: str, payload: Mapping[str, Any]) -> Event:
            nonlocal next_seq, prev_hash
            event = Event.create(
                run_id=self.run_id,
                seq=next_seq,
                tick=tick,
                type=event_type,
                payload=payload,
                prev_hash=prev_hash,
            )
            result.append(event)
            next_seq += 1
            prev_hash = event.hash
            return event

        begin = add("tick_begin", {"event_count": len(user_events)})
        for event_type, payload in user_events:
            add(event_type, payload)
        state_hash = json_sha256(state)
        checkpoint = add(
            "state_checkpoint",
            {"state": state, "rng_state": rng_state, "state_hash": state_hash},
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
        return tuple(result)

    def load_latest_checkpoint(self) -> dict[str, Any] | None:
        """Return the latest committed opaque state and RNG checkpoint."""

        with self._mutex:
            self._ensure_usable()
            assert self._conn is not None
            row = self._conn.execute(
                """
                SELECT tick, state_json, rng_state_json, state_hash, event_seq
                FROM checkpoints ORDER BY tick DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            tick, state_json, rng_json, state_hash, event_seq = row
            try:
                state = strict_json_loads(state_json)
                rng_state = strict_json_loads(rng_json)
            except EventValidationError as exc:
                raise StorageIntegrityError(f"invalid checkpoint JSON: {exc}") from exc
            if not isinstance(state, dict) or not isinstance(rng_state, dict):
                raise StorageIntegrityError("checkpoint values are not JSON objects")
            if json_sha256(state) != state_hash:
                raise StorageIntegrityError("latest SQLite checkpoint hash mismatch")
            mechanical_state = state.get("world", state)
            if isinstance(mechanical_state, dict):
                embedded_rng = mechanical_state.get("rng_state")
                if embedded_rng is not None and embedded_rng != rng_state:
                    raise StorageIntegrityError(
                        "latest checkpoint RNG differs from embedded world state"
                    )
                embedded_tick = mechanical_state.get("tick")
                if embedded_tick is not None and embedded_tick != tick:
                    raise StorageIntegrityError(
                        "latest checkpoint tick differs from embedded world state"
                    )
            return {
                "tick": tick,
                "state": state,
                "rng_state": rng_state,
                "state_hash": state_hash,
                "event_seq": event_seq,
            }

    def verify(self) -> dict[str, Any]:
        """Verify the JSONL chain/protocol and its complete SQLite projection."""

        with self._mutex:
            self._ensure_usable()
            scan = self._scan_log()
            self._verify_sqlite(scan)
            return {
                "run_id": self.run_id,
                "schema_version": SCHEMA_VERSION,
                "committed_ticks": len(scan.transactions),
                "committed_events": len(scan.events),
                "last_tick": scan.last_tick,
                "last_seq": scan.last_seq,
                "last_hash": scan.last_hash,
                "incomplete_tail_bytes": scan.incomplete_tail_bytes,
                "sqlite": "ok",
            }

    def _verify_sqlite(self, scan: _ScanResult) -> None:
        assert self._conn is not None
        integrity = self._conn.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise StorageIntegrityError(f"SQLite integrity_check failed: {integrity!r}")
        foreign_keys = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise StorageIntegrityError(f"SQLite foreign-key violations: {foreign_keys!r}")

        rows = self._conn.execute("SELECT seq, hash FROM events ORDER BY seq").fetchall()
        expected = [(event.seq, event.hash) for event in scan.events]
        if rows != expected:
            raise StorageIntegrityError("SQLite events projection differs from JSONL")
        checkpoints = self._conn.execute(
            "SELECT tick, event_seq, state_hash FROM checkpoints ORDER BY tick"
        ).fetchall()
        expected_checkpoints = [
            (event.tick, event.seq, event.payload["state_hash"])
            for event in scan.events
            if event.type == "state_checkpoint"
        ]
        if checkpoints != expected_checkpoints:
            raise StorageIntegrityError("SQLite checkpoints projection differs from JSONL")
        if scan.last_seq is not None:
            if self._get_meta(self._conn, "projected_seq") != scan.last_seq:
                raise StorageIntegrityError("SQLite projected_seq is stale or corrupt")
            if self._get_meta(self._conn, "projected_hash") != scan.last_hash:
                raise StorageIntegrityError("SQLite projected_hash is stale or corrupt")
        elif rows:
            raise StorageIntegrityError("SQLite is non-empty while JSONL is empty")

        # Reproject the authoritative log independently and compare every
        # derived byte, not only event IDs/checkpoint hashes.  SQLite integrity
        # checks cannot detect a semantically forged but well-formed row.
        expected_connection = sqlite3.connect(
            ":memory:", isolation_level=None, check_same_thread=True
        )
        try:
            expected_connection.execute("PRAGMA foreign_keys = ON")
            expected_connection.execute("PRAGMA trusted_schema = OFF")
            self._initialize_schema(expected_connection)
            self._bind_database_to_run(expected_connection)
            self._project_transactions(expected_connection, scan.transactions)
            for table in _PROJECTION_TABLES:
                # `table` comes exclusively from the module constant above.
                query = f'SELECT * FROM "{table}" ORDER BY rowid'  # noqa: S608
                actual_rows = self._conn.execute(query).fetchall()
                expected_rows = expected_connection.execute(query).fetchall()
                if actual_rows != expected_rows:
                    raise StorageIntegrityError(
                        f"SQLite projection table {table!r} differs from JSONL"
                    )
        finally:
            expected_connection.close()

    def rebuild_sqlite(self) -> None:
        """Atomically build a fresh SQLite projection from committed JSONL."""

        with self._mutex:
            self._ensure_usable()
            scan = self._scan_log()
            if scan.incomplete_tail_bytes:
                raise StorageIntegrityError(
                    "refusing rebuild while JSONL has an incomplete tail; reopen to recover"
                )
            token = secrets.token_hex(16)
            temporary = self.run_dir / f".{SQLITE_NAME}.rebuild-{token}"
            temp_connection: sqlite3.Connection | None = None
            try:
                temp_connection = _open_sqlite(temporary, wal=False)
                self._initialize_schema(temp_connection)
                self._bind_database_to_run(temp_connection)
                self._project_transactions(temp_connection, scan.transactions)
                integrity = temp_connection.execute("PRAGMA integrity_check").fetchone()
                if integrity != ("ok",):
                    raise StorageIntegrityError("rebuilt SQLite failed integrity_check")
                temp_connection.close()
                temp_connection = None
                fd = os.open(temporary, os.O_RDONLY | _SAFE_OPEN_FLAGS)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                assert self._conn is not None
                self._conn.close()
                self._conn = None
                _safe_unlink(Path(f"{self.sqlite_path}-wal"))
                _safe_unlink(Path(f"{self.sqlite_path}-shm"))
                os.replace(temporary, self.sqlite_path)
                os.chmod(self.sqlite_path, 0o600)
                _fsync_directory(self.run_dir)
                self._conn = _open_sqlite(self.sqlite_path, wal=True)
                self._initialize_schema(self._conn)
                self._bind_database_to_run(self._conn)
                self._verify_sqlite(scan)
            except BaseException:
                if temp_connection is not None:
                    temp_connection.close()
                _safe_unlink(temporary)
                _safe_unlink(Path(f"{temporary}-journal"))
                if self._conn is None:
                    # Best effort: leave the object usable if replacement did
                    # not happen; otherwise the raised error is still explicit.
                    try:
                        self._conn = _open_sqlite(self.sqlite_path, wal=True)
                    except BaseException:
                        self._poisoned = True
                raise

    def append_raw(
        self,
        generation: str | int,
        agent: str | int,
        tick: int,
        response: Any,
        *,
        channel: str = "act",
        response_id: str | None = None,
    ) -> Path:
        """Atomically store one immutable raw model response.

        Identifiers are opaque metadata.  The filename is a digest of them plus
        a random nonce, so traversal strings can never influence the path.
        """

        with self._mutex:
            self._ensure_usable()
            generation_value = self._opaque_id(generation, "generation")
            agent_value = self._opaque_id(agent, "agent")
            if type(tick) is not int or tick < 0:
                raise RawResponseError("tick must be a non-negative integer")
            if not isinstance(channel, str) or not channel or len(channel) > 128:
                raise RawResponseError("channel must be a non-empty string <= 128 characters")
            if response_id is None:
                response_id = secrets.token_hex(16)
            elif not isinstance(response_id, str) or not response_id or len(response_id) > 512:
                raise RawResponseError("response_id must be a non-empty opaque string")

            if isinstance(response, bytes):
                response_value: Any = {
                    "encoding": "base64",
                    "data": base64.b64encode(response).decode("ascii"),
                }
            else:
                try:
                    response_value = strict_json_loads(canonical_json(response))
                except EventValidationError as exc:
                    raise RawResponseError(f"response is not finite JSON: {exc}") from exc
            record = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "generation": generation_value,
                "agent": agent_value,
                "tick": tick,
                "channel": channel,
                "response_id": response_id,
                "response": response_value,
            }
            try:
                data = canonical_json_bytes(record) + b"\n"
            except (EventValidationError, UnicodeError) as exc:
                # A hostile response can pass json.loads yet still be
                # unencodable UTF-8 (lone surrogate escapes).  Raw capture must
                # drop it as a recorded rejection, never crash the tick.
                raise RawResponseError(f"response is not encodable JSON: {exc}") from exc
            if len(data) > self.max_raw_bytes:
                raise RawResponseError(
                    f"raw response is {len(data)} bytes; limit is {self.max_raw_bytes}"
                )
            digest = json_sha256(
                {
                    "run_id": self.run_id,
                    "generation": generation_value,
                    "agent": agent_value,
                    "tick": tick,
                    "channel": channel,
                    "response_id": response_id,
                }
            )
            return self._atomic_raw_write(f"{digest}.json", data)

    @staticmethod
    def _opaque_id(value: str | int, name: str) -> str | int:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise RawResponseError(f"{name} must be an opaque string or integer")
        if isinstance(value, str) and (not value or len(value) > 512):
            raise RawResponseError(f"{name} must contain 1-512 characters")
        return value

    def _atomic_raw_write(self, filename: str, data: bytes) -> Path:
        raw_dir = _ensure_directory(self.raw_dir)
        dir_flags = (
            os.O_RDONLY | _os_flag("O_DIRECTORY") | _os_flag("O_CLOEXEC") | _os_flag("O_NOFOLLOW")
        )
        dir_fd = os.open(raw_dir, dir_flags)
        temp_name = f".tmp-{secrets.token_hex(16)}"
        temp_created = False
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _SAFE_OPEN_FLAGS,
                0o600,
                dir_fd=dir_fd,
            )
            temp_created = True
            try:
                _write_all(fd, data)
                os.fsync(fd)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            try:
                os.link(
                    temp_name,
                    filename,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise RawResponseError("raw response ID already exists") from exc
            os.unlink(temp_name, dir_fd=dir_fd)
            temp_created = False
            os.fsync(dir_fd)
        finally:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            os.close(dir_fd)
        return raw_dir / filename

    def close(self) -> None:
        """Flush handles and release the single-writer lock."""

        with self._mutex:
            if self._closed:
                return
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._event_fd is not None:
                os.close(self._event_fd)
                self._event_fd = None
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._lock_fd)
                    self._lock_fd = None

    def __enter__(self) -> EventStore:
        self._ensure_usable()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            return


# CSI, OSC and the other string escape families that can alter a terminal even
# when their visible payload looks harmless.
_ANSI_ESCAPE_RE = re.compile(
    r"(?:"
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[P^_X][\s\S]*?\x1b\\"  # DCS/PM/APC/SOS
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[@-~]"  # two-byte escape
    r")"
)


def sanitize_control_text(
    text: str, *, replacement: str = "", max_length: int | None = None
) -> str:
    """Strip ANSI escapes and non-printing controls before terminal/UI output.

    Newline and tab are retained.  Unicode formatting controls (including bidi
    overrides) are removed as well.  Raw forensic files should keep the original
    response and call this helper only at display boundaries.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(replacement, str):
        raise TypeError("replacement must be a string")
    if max_length is not None and (type(max_length) is not int or max_length < 0):
        raise ValueError("max_length must be a non-negative integer or None")
    without_ansi = _ANSI_ESCAPE_RE.sub(replacement, text).replace("\x1b", replacement)
    cleaned = "".join(
        character
        if character in {"\n", "\t"} or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        else replacement
        for character in without_ansi
    )
    return cleaned if max_length is None else cleaned[:max_length]


sanitize_for_terminal = sanitize_control_text


__all__ = [
    "DEFAULT_MAX_EVENT_LINE_BYTES",
    "DEFAULT_MAX_RAW_BYTES",
    "EVENT_LOG_NAME",
    "LOCK_NAME",
    "RAW_DIRECTORY_NAME",
    "RECOVERY_DIRECTORY_NAME",
    "RESERVED_EVENT_TYPES",
    "SQLITE_NAME",
    "EventStore",
    "ProjectionError",
    "RawResponseError",
    "StorageError",
    "StorageIntegrityError",
    "StoreLockedError",
    "sanitize_control_text",
    "sanitize_for_terminal",
]
