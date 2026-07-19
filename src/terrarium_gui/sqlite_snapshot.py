"""Cached SQLite/WAL snapshots that never coordinate in a run directory."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_READ_FLAGS = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
    getattr(os, "O_NOFOLLOW", 0)
)
type Fingerprint = tuple[int, int, int, int, int]
type ProjectionFingerprint = tuple[Fingerprint, Fingerprint | None]


class SnapshotBusyError(OSError):
    """The source projection changed while its read-only copy was made."""


@dataclass(slots=True)
class _SnapshotEntry:
    fingerprint: ProjectionFingerprint
    directory: Path
    database: Path
    has_wal: bool
    users: int = 0
    stale: bool = False


class _ProjectionSnapshotCache:
    """Share stable outside-run copies and reuse an unchanged DB base image."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="terrarium-gui-projections-")
        self.root = Path(self._temporary.name)
        self._lock = threading.RLock()
        self._current: dict[Path, _SnapshotEntry] = {}
        self._db_blobs: dict[Fingerprint, Path] = {}
        self._serial = 0

    @contextmanager
    def acquire(self, source: Path, *, attempts: int) -> Iterator[_SnapshotEntry]:
        source = source.absolute()
        with self._lock:
            entry = self._select_or_build(source, attempts=attempts)
            entry.users += 1
        try:
            yield entry
        finally:
            with self._lock:
                entry.users -= 1
                if entry.stale and entry.users == 0:
                    shutil.rmtree(entry.directory, ignore_errors=True)

    def _select_or_build(self, source: Path, *, attempts: int) -> _SnapshotEntry:
        last_error: BaseException | None = None
        for attempt in range(attempts):
            db_fd: int | None = None
            wal_fd: int | None = None
            try:
                db_fd = _open_regular(source)
                wal_path = source.with_name(f"{source.name}-wal")
                wal_fd = _open_optional_regular(wal_path)
                before = (
                    _fingerprint_fd(db_fd),
                    _fingerprint_fd(wal_fd) if wal_fd is not None else None,
                )
                current = self._current.get(source)
                if current is not None and current.fingerprint == before:
                    if _fds_and_paths_still_match(source, wal_path, db_fd, wal_fd, before):
                        return current
                    raise SnapshotBusyError("projection changed during cache lookup")

                entry = self._build_entry(source, wal_path, db_fd, wal_fd, before)
                old = self._current.get(source)
                self._current[source] = entry
                if old is not None:
                    old.stale = True
                    if old.users == 0:
                        shutil.rmtree(old.directory, ignore_errors=True)
                return entry
            except (SnapshotBusyError, OSError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(0.005 * (attempt + 1))
            finally:
                if db_fd is not None:
                    os.close(db_fd)
                if wal_fd is not None:
                    os.close(wal_fd)
        raise sqlite3.OperationalError(
            "SQLite projection snapshot is unavailable"
        ) from last_error

    def _build_entry(
        self,
        source: Path,
        wal_path: Path,
        db_fd: int,
        wal_fd: int | None,
        before: ProjectionFingerprint,
    ) -> _SnapshotEntry:
        self._serial += 1
        directory = self.root / f"snapshot-{self._serial:016x}"
        directory.mkdir(mode=0o700)
        target = directory / source.name
        try:
            db_fingerprint = before[0]
            blob = self._db_blobs.get(db_fingerprint)
            if blob is None or not blob.is_file():
                blob = self.root / f"db-{len(self._db_blobs):016x}"
                _copy_fd(db_fd, blob)
                os.chmod(blob, 0o400)
                self._db_blobs[db_fingerprint] = blob
            os.link(blob, target)
            if wal_fd is not None:
                target_wal = target.with_name(f"{target.name}-wal")
                _copy_fd(wal_fd, target_wal)
                os.chmod(target_wal, 0o400)
            if not _fds_and_paths_still_match(
                source, wal_path, db_fd, wal_fd, before
            ):
                raise SnapshotBusyError("projection changed during snapshot")
            return _SnapshotEntry(
                fingerprint=before,
                directory=directory,
                database=target,
                has_wal=wal_fd is not None,
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise


_SNAPSHOTS = _ProjectionSnapshotCache()


@contextmanager
def open_projection_snapshot(
    source: str | Path,
    *,
    timeout: float = 0,
    attempts: int = 4,
) -> Iterator[sqlite3.Connection]:
    """Open a stable GUI-owned copy of a DB and its optional WAL.

    SQLite ``mode=ro`` WAL clients can update read marks in ``-shm``.  Here all
    SQLite coordination occurs under a private temporary root.  Unchanged
    snapshots are shared across queries, and when only WAL advances the large
    base database is hard-linked from a previously verified GUI-owned copy.
    """

    with _SNAPSHOTS.acquire(Path(source), attempts=attempts) as snapshot:
        options = "mode=ro" if snapshot.has_wal else "mode=ro&immutable=1"
        uri = f"file:{quote(snapshot.database.as_posix(), safe='/')}?{options}"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA busy_timeout=0")
            yield connection
        finally:
            connection.close()


def _fds_and_paths_still_match(
    database: Path,
    wal: Path,
    db_fd: int,
    wal_fd: int | None,
    expected: ProjectionFingerprint,
) -> bool:
    current = (
        _fingerprint_fd(db_fd),
        _fingerprint_fd(wal_fd) if wal_fd is not None else None,
    )
    return (
        current == expected
        and _path_matches(database, expected[0])
        and _optional_path_matches(wal, expected[1])
    )


def _open_regular(path: Path) -> int:
    fd = os.open(path, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("SQLite projection source is not a private regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_optional_regular(path: Path) -> int | None:
    try:
        return _open_regular(path)
    except FileNotFoundError:
        return None


def _fingerprint_fd(fd: int) -> Fingerprint:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _path_matches(path: Path, expected: Fingerprint) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_nlink == 1
        and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        == expected
    )


def _optional_path_matches(path: Path, expected: Fingerprint | None) -> bool:
    if expected is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False
    return _path_matches(path, expected)


def _copy_fd(source_fd: int, target: Path) -> None:
    target_fd = os.open(
        target,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0)),
        0o600,
    )
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(target_fd, remaining)
                if written <= 0:
                    raise OSError("short write while copying SQLite projection")
                remaining = remaining[written:]
    finally:
        os.close(target_fd)


__all__ = ["open_projection_snapshot"]
