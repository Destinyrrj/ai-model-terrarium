"""Read-only discovery and status classification for sealed run directories."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import sqlite3
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from terrarium.events import HASH_RE
from terrarium.replay import load_manifest_config
from terrarium.storage import LOCK_NAME

from .event_cache import VerifiedEventCache
from .sqlite_snapshot import open_projection_snapshot
from .tailer import TailerError

# Keep directory tokens deliberately narrower than RunConfig.run_id.  A run name
# is an operator-facing local identifier, never a path supplied by a client.
RUN_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_VERIFIED_EVENTS = VerifiedEventCache()


class InvalidRunName(ValueError):
    """A client supplied something other than a single safe directory token."""


class RunNotFound(FileNotFoundError):
    """A named run is not a real direct child of the configured runs root."""


def validate_run_name(value: str) -> str:
    if not isinstance(value, str) or RUN_NAME_RE.fullmatch(value) is None:
        raise InvalidRunName("run name must be a safe directory token")
    return value


@dataclass(frozen=True, slots=True)
class RunRecord:
    name: str
    status: str
    run_id: str | None
    last_tick: int | None
    last_seq: int | None
    last_hash: str | None
    target_generation: int | None
    current_generation: int | None
    completed: bool
    has_manifest: bool
    has_events: bool
    has_projection: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class RunRegistry:
    """Discover runs without opening :class:`terrarium.storage.EventStore`.

    The registry never creates a file below a run directory.  SQLite is opened
    with ``mode=ro`` and writer activity is detected by probing the existing
    flock file through a no-follow read-only descriptor.
    """

    def __init__(self, runs_root: str | Path) -> None:
        self.runs_root = Path(runs_root).absolute()
        self.runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.runs_root.is_symlink() or not self.runs_root.is_dir():
            raise ValueError("runs root must be a real directory")
        self.runs_root = self.runs_root.resolve(strict=True)

    def candidate(self, name: str) -> Path:
        """Return a safe direct-child path, whether or not it exists yet."""

        token = validate_run_name(name)
        return self.runs_root / token

    def resolve(self, name: str, *, require_manifest: bool = False) -> Path:
        candidate = self.candidate(name)
        try:
            info = candidate.lstat()
        except FileNotFoundError as exc:
            raise RunNotFound(name) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RunNotFound(name)
        resolved = candidate.resolve(strict=True)
        if resolved.parent != self.runs_root:
            raise RunNotFound(name)
        if require_manifest and not _is_private_regular(resolved / "manifest.json"):
            raise RunNotFound(name)
        return resolved

    def list(self, *, managed_running: set[str] | None = None) -> list[RunRecord]:
        managed = managed_running or set()
        records: list[RunRecord] = []
        for child in sorted(self.runs_root.iterdir(), key=lambda item: item.name):
            if RUN_NAME_RE.fullmatch(child.name) is None:
                continue
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                records.append(self.inspect(child.name, managed_running=child.name in managed))
        return records

    def inspect(self, name: str, *, managed_running: bool = False) -> RunRecord:
        root = self.resolve(name)
        manifest_path = root / "manifest.json"
        events_path = root / "events.jsonl"
        sqlite_path = root / "state.sqlite3"
        has_manifest = _is_private_regular(manifest_path)
        has_events = _is_private_regular(events_path)
        has_projection = _is_private_regular(sqlite_path)

        external_writer = self.writer_active(root)
        run_id: str | None = None
        target: int | None = None
        expected_lineages: int | None = None
        last_tick: int | None = None
        last_seq: int | None = None
        last_hash: str | None = None
        current_generation: int | None = None
        complete = False
        error: str | None = None
        manifest_valid = False
        projection_valid = False
        committed_identity = False

        if has_manifest:
            try:
                config = load_manifest_config(root)
                run_id = config.run_id
                target = config.population.generations - 1
                expected_lineages = config.population.size
                manifest_valid = True
            except Exception:  # untrusted/corrupt historical directory
                error = "invalid_manifest"
        if has_projection:
            try:
                summary = _projection_summary(sqlite_path)
                last_tick = summary["last_tick"]
                last_seq = summary["last_seq"]
                last_hash = summary["last_hash"]
                current_generation = summary["current_generation"]
                projection_valid = True
                committed_identity = _has_committed_identity(summary)
                if not committed_identity:
                    error = error or "no_committed_checkpoint"
                if target is not None and expected_lineages is not None:
                    lineages = summary["lineage_generations"]
                    complete = (
                        has_events
                        and committed_identity
                        and len(lineages) == expected_lineages
                        and all(generation >= target for generation in lineages)
                    )
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
                error = error or "projection_unavailable"
        elif manifest_valid and has_events:
            error = error or "projection_missing"
        if manifest_valid and has_projection and not has_events:
            error = error or "event_log_missing"

        if manifest_valid and has_events and not external_writer:
            try:
                json_tick, json_seq, json_hash = _VERIFIED_EVENTS.identity(events_path)
            except TailerError:
                error = "event_log_invalid"
                complete = False
            else:
                if (
                    projection_valid
                    and type(json_seq) is int
                    and isinstance(json_hash, str)
                    and (last_tick, last_seq, last_hash)
                    != (json_tick, json_seq, json_hash)
                ):
                    error = "projection_stale"
                    complete = False
                    # Invocation budgets and dashboard cursors must start at
                    # the authoritative fsync-backed JSONL commit, not at the
                    # replaceable projection that core will repair on reopen.
                    last_tick = json_tick
                    last_seq = json_seq
                    last_hash = json_hash

        resumable = (
            manifest_valid
            and has_events
            and has_projection
            and projection_valid
            and committed_identity
        )

        if managed_running:
            status = "running"
        elif external_writer:
            status = "external"
        elif error in {
            "invalid_manifest",
            "projection_unavailable",
            "event_log_missing",
            "event_log_invalid",
        }:
            status = "invalid"
        elif complete:
            status = "completed"
        elif resumable:
            status = "resumable"
        else:
            status = "incomplete"
        return RunRecord(
            name=name,
            status=status,
            run_id=run_id,
            last_tick=last_tick,
            last_seq=last_seq,
            last_hash=last_hash,
            target_generation=target,
            current_generation=current_generation,
            completed=complete,
            has_manifest=has_manifest,
            has_events=has_events,
            has_projection=has_projection,
            error=error,
        )

    @staticmethod
    def writer_active(root: Path) -> bool:
        lock = root / LOCK_NAME
        try:
            info = lock.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            return exc.errno != errno.ENOENT
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return True
        flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
            getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(lock, flags)
        except OSError as exc:
            return exc.errno != errno.ENOENT
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return True
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
        finally:
            os.close(fd)


def _is_private_regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _projection_summary(path: Path) -> dict[str, Any]:
    with open_projection_snapshot(path, timeout=0.15) as connection:
        last = connection.execute(
            "SELECT tick, seq, hash, type FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT tick, event_seq, state_json FROM checkpoints ORDER BY tick DESC LIMIT 1"
        ).fetchone()
    generations: list[int] = []
    if checkpoint is not None:
        state = json.loads(checkpoint[2])
        raw = state.get("lineages") if isinstance(state, dict) else None
        if isinstance(raw, dict):
            for lineage in raw.values():
                if isinstance(lineage, dict) and type(lineage.get("generation")) is int:
                    generations.append(lineage["generation"])
    return {
        "last_tick": last[0] if last else None,
        "last_seq": last[1] if last else None,
        "last_hash": last[2] if last else None,
        "last_type": last[3] if last else None,
        "checkpoint_tick": checkpoint[0] if checkpoint else None,
        "checkpoint_event_seq": checkpoint[1] if checkpoint else None,
        "lineage_generations": generations,
        "current_generation": min(generations) if generations else None,
    }


def _has_committed_identity(summary: dict[str, Any]) -> bool:
    last_tick = summary.get("last_tick")
    last_seq = summary.get("last_seq")
    last_hash = summary.get("last_hash")
    checkpoint_tick = summary.get("checkpoint_tick")
    checkpoint_event_seq = summary.get("checkpoint_event_seq")
    return (
        type(last_tick) is int
        and last_tick >= 0
        and type(last_seq) is int
        and last_seq >= 0
        and isinstance(last_hash, str)
        and HASH_RE.fullmatch(last_hash) is not None
        and summary.get("last_type") == "tick_commit"
        and checkpoint_tick == last_tick
        and type(checkpoint_event_seq) is int
        and 0 <= checkpoint_event_seq < last_seq
    )


def _safe_sqlite_sidecar(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise sqlite3.OperationalError("unsafe SQLite sidecar")
    return True


__all__ = [
    "RUN_NAME_RE",
    "InvalidRunName",
    "RunNotFound",
    "RunRecord",
    "RunRegistry",
    "validate_run_name",
]
