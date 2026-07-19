"""Lifecycle management for Terrarium CLI subprocesses.

Only the sealed CLI process writes a run directory.  GUI-owned snapshots,
stderr/stdout captures and history live beneath the separate artifacts root.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from terrarium.config import MAX_CONFIG_BYTES, RunConfig, load_config
from terrarium.replay import load_manifest_config
from terrarium.storage import sanitize_control_text

from .registry import RunRegistry, validate_run_name


class ProcessConflict(RuntimeError):
    """A lifecycle operation conflicts with current run state."""


class ManagedProcessNotFound(LookupError):
    """The GUI did not launch a currently running process for this run."""


@dataclass(slots=True)
class ManagedRun:
    job_id: str
    run_name: str
    command: tuple[str, ...]
    snapshot_path: Path
    stdout_path: Path
    stderr_path: Path
    process: asyncio.subprocess.Process
    started_at: str
    max_ticks: int | None
    invocation_start_tick: int | None
    invocation_target_tick: int | None
    ended_at: str | None = None
    returncode: int | None = None
    result: dict[str, Any] | None = None
    history_error: str | None = None
    _monitor: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        # The subprocess object exposes its return code before the monitor has
        # parsed stdout/stderr and finalized this public snapshot.  Keep the
        # job active until all terminal fields are published together.
        return self.ended_at is None

    def public(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "run_name": self.run_name,
            "pid": self.process.pid,
            "running": self.running,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "returncode": self.returncode,
            "max_ticks": self.max_ticks,
            "invocation_start_tick": self.invocation_start_tick,
            "invocation_target_tick": self.invocation_target_tick,
            "result": self.result,
            "history_error": self.history_error,
        }


class ProcessManager:
    def __init__(
        self,
        registry: RunRegistry,
        artifacts_root: str | Path,
        *,
        interrupt_timeout: float = 10.0,
        terminate_timeout: float = 5.0,
    ) -> None:
        self.registry = registry
        self.artifacts_root = Path(artifacts_root).absolute()
        self.artifacts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.artifacts_root.is_symlink() or not self.artifacts_root.is_dir():
            raise ValueError("artifacts root must be a real directory")
        self.artifacts_root = self.artifacts_root.resolve(strict=True)
        self.interrupt_timeout = interrupt_timeout
        self.terminate_timeout = terminate_timeout
        self._runs: dict[str, ManagedRun] = {}
        self._jobs: dict[str, ManagedRun] = {}
        self._audit_reservations: set[str] = set()
        self._lock = asyncio.Lock()
        self._history_lock = asyncio.Lock()

    def running_names(self) -> set[str]:
        return {name for name, handle in self._runs.items() if handle.running}

    def known_names(self) -> set[str]:
        """Run names launched during this GUI session, including failures."""

        return set(self._runs)

    def is_running(self, run_name: str) -> bool:
        handle = self._runs.get(validate_run_name(run_name))
        return bool(handle and handle.running)

    def get(self, job_id: str) -> ManagedRun | None:
        return self._jobs.get(job_id)

    def get_for_run(self, run_name: str) -> ManagedRun | None:
        return self._runs.get(validate_run_name(run_name))

    async def start(
        self,
        *,
        run_name: str,
        config_path: str | Path | None = None,
        config: RunConfig | None = None,
        config_bytes: bytes | None = None,
        max_ticks: int | None = None,
        resume: bool = False,
    ) -> ManagedRun:
        name = validate_run_name(run_name)
        if max_ticks is not None and (
            isinstance(max_ticks, bool)
            or not isinstance(max_ticks, int)
            or not 0 <= max_ticks <= (1 << 63) - 1
        ):
            raise ValueError("max_ticks must fit a non-negative signed 64-bit integer")

        async with self._lock:
            current = self._runs.get(name)
            if current is not None and current.running:
                raise ProcessConflict("run already has a managed process")
            if name in self._audit_reservations:
                raise ProcessConflict("run has an active audit reservation")
            candidate = self.registry.candidate(name)
            if resume:
                root = self.registry.resolve(name, require_manifest=True)
                if self.registry.writer_active(root):
                    raise ProcessConflict("run already has an active writer")
                sealed = load_manifest_config(root)
                invocation_start_tick = self.registry.inspect(name).last_tick
            else:
                if candidate.exists() or candidate.is_symlink():
                    raise ProcessConflict("run directory already exists")
                if config is not None:
                    sealed = config
                elif config_path is not None:
                    sealed = load_config(config_path)
                else:
                    raise ValueError("configuration is required for a new run")
                if config_bytes is not None and (
                    not isinstance(config_bytes, bytes)
                    or len(config_bytes) > MAX_CONFIG_BYTES
                ):
                    raise ValueError("config_bytes exceeds the configuration limit")
                # Terrarium initialization durably creates tick zero before the
                # additional-commit run budget begins.
                invocation_start_tick = 0
            invocation_target_tick = (
                invocation_start_tick + max_ticks
                if invocation_start_tick is not None and max_ticks is not None
                else None
            )

            snapshot_source: str | Path | bytes | None
            if resume:
                snapshot_source = None
            elif config_bytes is not None:
                snapshot_source = config_bytes
            else:
                snapshot_source = config_path
            snapshot = self._prepare_snapshot(name, sealed, snapshot_source)
            # Re-parse the exact GUI snapshot before handing it to the sealed CLI.
            snap_config = load_config(snapshot)
            if snap_config.canonical_bytes() != sealed.canonical_bytes():
                raise RuntimeError("GUI config snapshot differs from sealed configuration")

            job_id = secrets.token_urlsafe(18)
            job_dir = self._job_directory(job_id)
            stdout_path = job_dir / "stdout.jsonl"
            stderr_path = job_dir / "stderr.log"
            command = [
                sys.executable,
                "-m",
                "terrarium.cli",
                "run",
                str(snapshot),
                "--output",
                str(candidate),
            ]
            if max_ticks is not None:
                command.extend(("--max-ticks", str(max_ticks)))

            stdout_handle = stdout_path.open("wb")
            stderr_handle = stderr_path.open("wb")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
            finally:
                stdout_handle.close()
                stderr_handle.close()
            handle = ManagedRun(
                job_id=job_id,
                run_name=name,
                command=tuple(command),
                snapshot_path=snapshot,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                process=process,
                started_at=_now(),
                max_ticks=max_ticks,
                invocation_start_tick=invocation_start_tick,
                invocation_target_tick=invocation_target_tick,
            )
            self._runs[name] = handle
            self._jobs[job_id] = handle
            handle._monitor = asyncio.create_task(
                self._monitor(handle), name=f"terrarium-run-{name}-{job_id}"
            )
            # From this point onward the child is always monitored.  GUI audit
            # history is useful evidence, but a disk/fsync failure in the
            # separate artifacts directory must never orphan a sealed writer.
            await self._record_history("started", handle)
            return handle

    async def resume(self, run_name: str, *, max_ticks: int | None = None) -> ManagedRun:
        return await self.start(run_name=run_name, max_ticks=max_ticks, resume=True)

    async def stop(self, run_name: str) -> ManagedRun:
        name = validate_run_name(run_name)
        async with self._lock:
            handle = self._runs.get(name)
            if handle is None or not handle.running:
                raise ManagedProcessNotFound("run is not active in this GUI session")
            _signal_group(handle.process.pid, signal.SIGINT)
            await self._record_history("interrupt_requested", handle)
        await self._wait_or_signal(handle, self.interrupt_timeout, signal.SIGTERM)
        if handle.running:
            await self._wait_or_signal(handle, self.terminate_timeout, signal.SIGKILL)
        if handle.running:
            await handle.process.wait()
        if handle._monitor is not None:
            await asyncio.shield(handle._monitor)
        return handle

    async def shutdown(self) -> None:
        """Durably stop GUI-owned children before the server exits."""

        failures: list[Exception] = []
        for name in sorted(self.running_names()):
            try:
                await self.stop(name)
            except Exception as exc:
                # One broken process/history artifact must not prevent the
                # remaining GUI-owned writer groups from receiving shutdown.
                failures.append(exc)
        if failures:
            raise ExceptionGroup("one or more GUI-owned processes failed to stop", failures)

    async def reserve_audit(self, run_name: str) -> None:
        """Serialize audit dispatch with managed writer dispatch for one run."""

        name = validate_run_name(run_name)
        async with self._lock:
            current = self._runs.get(name)
            if current is not None and current.running:
                raise ProcessConflict("run has a managed writer")
            if name in self._audit_reservations:
                raise ProcessConflict("run already has an audit reservation")
            self._audit_reservations.add(name)

    async def release_audit(self, run_name: str) -> None:
        name = validate_run_name(run_name)
        async with self._lock:
            self._audit_reservations.discard(name)

    def stderr_tail(self, run_name: str, *, max_bytes: int = 64 * 1024) -> str:
        handle = self.get_for_run(run_name)
        if handle is None:
            return ""
        raw = _bounded_tail(handle.stderr_path, max_bytes=max_bytes)
        return sanitize_control_text(raw.decode("utf-8", errors="replace"), max_length=max_bytes)

    async def _monitor(self, handle: ManagedRun) -> None:
        returncode = await handle.process.wait()
        handle.returncode = returncode
        handle.result = _read_cli_result(handle.stdout_path, handle.stderr_path, returncode)
        # No await between result and ended_at: readers cannot observe a
        # terminal job with missing returncode/result.
        handle.ended_at = _now()
        await self._record_history("finished", handle)

    async def _wait_or_signal(
        self, handle: ManagedRun, wait_seconds: float, next_signal: signal.Signals
    ) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(handle.process.wait()), timeout=wait_seconds)
        except TimeoutError:
            if handle.running:
                _signal_group(handle.process.pid, next_signal)
                await self._record_history(f"signal_{next_signal.name.lower()}", handle)

    def _prepare_snapshot(
        self, name: str, config: RunConfig, source: str | Path | bytes | None
    ) -> Path:
        runs_directory = _safe_directory(self.artifacts_root, "runs")
        directory = _safe_directory(runs_directory, name)
        snapshot = directory / "config.snapshot.json"
        encoded: bytes
        if source is None:
            encoded = config.model_dump_json(indent=2).encode("utf-8") + b"\n"
        elif isinstance(source, bytes):
            encoded = source
        else:
            encoded = _read_config_bytes(Path(source))
        if snapshot.exists():
            existing = load_config(snapshot)
            if existing.canonical_bytes() != config.canonical_bytes():
                raise ProcessConflict("stored GUI config snapshot differs from requested config")
            return snapshot
        temporary = directory / f".config-{secrets.token_hex(12)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, snapshot)
            _fsync_directory(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return snapshot

    def _job_directory(self, job_id: str) -> Path:
        jobs_directory = _safe_directory(self.artifacts_root, "jobs")
        return _safe_directory(jobs_directory, job_id, exist_ok=False)

    async def _append_history(self, event: str, handle: ManagedRun) -> None:
        entry = {
            "event": event,
            "at": _now(),
            "job_id": handle.job_id,
            "run_name": handle.run_name,
            "pid": handle.process.pid,
            "returncode": handle.process.returncode,
            "max_ticks": handle.max_ticks,
        }
        encoded = (json.dumps(entry, sort_keys=True, allow_nan=False) + "\n").encode()
        history = self.artifacts_root / "history.jsonl"
        async with self._history_lock:
            fd = os.open(
                history,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | int(getattr(os, "O_CLOEXEC", 0))
                | int(getattr(os, "O_NOFOLLOW", 0)),
                0o600,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError("GUI history must be a private regular file")
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("short write while persisting GUI history")
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)

    async def _record_history(self, event: str, handle: ManagedRun) -> None:
        """Record GUI lifecycle evidence without controlling the child by it."""

        try:
            await self._append_history(event, handle)
        except Exception:
            # Keep the public contract stable and do not expose a hostile or
            # platform-specific exception string through the API.
            handle.history_error = "history_unavailable"


def _signal_group(pid: int, requested: signal.Signals) -> None:
    try:
        os.killpg(pid, requested)
    except ProcessLookupError:
        return


def _read_config_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError("configuration snapshot source is invalid")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("configuration snapshot source exceeds limit")
        return data
    finally:
        os.close(fd)


def _bounded_tail(path: Path, *, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            return stream.read(max_bytes)
    except FileNotFoundError:
        return b""


def _read_cli_result(stdout: Path, stderr: Path, returncode: int) -> dict[str, Any]:
    source = stdout if returncode == 0 else stderr
    text = _bounded_tail(source, max_bytes=256 * 1024).decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if lines:
        try:
            value = json.loads(lines[-1])
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    return {
        "status": "error" if returncode else "ok",
        "error": "process_failed" if returncode else None,
        "returncode": returncode,
    }


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_directory(parent: Path, name: str, *, exist_ok: bool = True) -> Path:
    candidate = parent / name
    candidate.mkdir(mode=0o700, exist_ok=exist_ok)
    info = candidate.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("GUI artifact directory is unsafe")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != parent.resolve(strict=True):
        raise RuntimeError("GUI artifact directory escaped its parent")
    return resolved


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ManagedProcessNotFound",
    "ManagedRun",
    "ProcessConflict",
    "ProcessManager",
]
