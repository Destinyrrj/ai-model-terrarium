"""Asynchronous wrappers around sealed Terrarium audit CLI commands."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from terrarium.replay import load_manifest_config
from terrarium.storage import sanitize_control_text

from .process_manager import ProcessConflict, ProcessManager
from .registry import RunRegistry, validate_run_name

ToolName = Literal["verify", "rebuild", "replay", "measure", "viewer-export"]
TOOLS = frozenset({"verify", "rebuild", "replay", "measure", "viewer-export"})


class ToolConflict(RuntimeError):
    """Audit tools cannot take the sealed writer lock while a run is active."""


@dataclass(slots=True)
class ToolJob:
    job_id: str
    run_name: str
    tool: ToolName
    status: str
    created_at: str
    artifact_dir: Path
    started_at: str | None = None
    ended_at: str | None = None
    returncode: int | None = None
    result: dict[str, Any] | None = None
    input_identity: tuple[int, str] | None = None
    measurement_identity: tuple[int, str] | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None

    def public(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "run_name": self.run_name,
            "tool": self.tool,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "returncode": self.returncode,
            "result": self.result,
            "measurement_identity": (
                {
                    "last_seq": self.measurement_identity[0],
                    "last_hash": self.measurement_identity[1],
                }
                if self.measurement_identity is not None
                else None
            ),
            "has_artifact": (
                any(self.artifact_dir.iterdir()) if self.artifact_dir.exists() else False
            ),
        }


class ToolManager:
    def __init__(
        self,
        registry: RunRegistry,
        processes: ProcessManager,
        artifacts_root: str | Path,
    ) -> None:
        self.registry = registry
        self.processes = processes
        artifacts = Path(artifacts_root).absolute().resolve(strict=True)
        candidate = artifacts / "tools"
        candidate.mkdir(mode=0o700, exist_ok=True)
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("tool artifacts directory must be a real directory")
        self.root = candidate.resolve(strict=True)
        if self.root.parent != artifacts:
            raise ValueError("tool artifacts directory escaped its root")
        self._jobs: dict[str, ToolJob] = {}
        self._lock = asyncio.Lock()

    async def submit(self, run_name: str, tool: str) -> ToolJob:
        name = validate_run_name(run_name)
        if tool not in TOOLS:
            raise ValueError("unsupported audit tool")
        typed_tool: ToolName = tool  # type: ignore[assignment]
        root = self.registry.resolve(name, require_manifest=True)
        async with self._lock:
            if any(
                job.run_name == name and job.status in {"queued", "running"}
                for job in self._jobs.values()
            ):
                raise ToolConflict("an audit job is already active for this run")
            try:
                await self.processes.reserve_audit(name)
            except ProcessConflict as exc:
                raise ToolConflict("run has an active writer or audit") from exc
            if self.registry.writer_active(root):
                await self.processes.release_audit(name)
                raise ToolConflict("run has an active writer")
            job_id = secrets.token_urlsafe(18)
            artifact_dir = self.root / job_id
            try:
                artifact_dir.mkdir(mode=0o700)
                identity = _run_identity(self.registry, name)
                job = ToolJob(
                    job_id=job_id,
                    run_name=name,
                    tool=typed_tool,
                    status="queued",
                    created_at=_now(),
                    artifact_dir=artifact_dir,
                    input_identity=identity,
                )
                self._jobs[job_id] = job
                job.task = asyncio.create_task(
                    self._execute(job, root), name=f"audit-{tool}-{name}"
                )
            except BaseException:
                await self.processes.release_audit(name)
                raise
            return job

    def get(self, job_id: str) -> ToolJob | None:
        return self._jobs.get(job_id)

    def list_for_run(self, run_name: str) -> list[ToolJob]:
        name = validate_run_name(run_name)
        return sorted(
            (job for job in self._jobs.values() if job.run_name == name),
            key=lambda job: (job.created_at, job.job_id),
        )

    async def measurement(
        self, run_name: str, metric: str
    ) -> dict[str, object]:
        """Return a verified cached CLI measurement or enqueue its generation."""

        name = validate_run_name(run_name)
        if metric not in {"behavior", "knowledge-survival"}:
            raise ValueError("unsupported measurement metric")
        identity = _run_identity(self.registry, name)
        if identity is None:
            raise ToolConflict("run projection has no committed identity")

        candidates = [
            job
            for job in self._jobs.values()
            if job.run_name == name
            and job.tool == "measure"
            and job.status == "succeeded"
            and job.measurement_identity == identity
        ]
        if candidates:
            latest = candidates[-1]
            filename = (
                "behavior-adoption.json"
                if metric == "behavior"
                else "knowledge-survival.json"
            )
            try:
                points = _read_measurement_json(
                    latest.artifact_dir / "measurement" / filename,
                )
            except (OSError, RuntimeError, json.JSONDecodeError, UnicodeError):
                # A successful subprocess is not a usable scientific result if
                # its bounded artifact disappeared or no longer validates.
                latest.status = "failed"
                latest.measurement_identity = None
                latest.result = {
                    "status": "error",
                    "error": "measurement_cache_invalid",
                }
                return {
                    "status": "failed",
                    "job_id": latest.job_id,
                    "error": "measurement_cache_invalid",
                    "retry": {
                        "method": "POST",
                        "path": f"/api/v1/runs/{name}/tools/measure",
                    },
                }
            return {
                "status": "ready",
                "points": points,
                "job_id": latest.job_id,
                "last_seq": identity[0],
                "last_hash": identity[1],
            }

        pending = next(
            (
                job
                for job in reversed(tuple(self._jobs.values()))
                if job.run_name == name
                and job.tool == "measure"
                and job.status in {"queued", "running"}
            ),
            None,
        )
        if pending is not None:
            return {"status": "pending", "job_id": pending.job_id}

        failed = next(
            (
                job
                for job in reversed(tuple(self._jobs.values()))
                if job.run_name == name
                and job.tool == "measure"
                and job.status in {"failed", "cancelled"}
                and job.input_identity == identity
            ),
            None,
        )
        if failed is not None:
            return {
                "status": "failed",
                "job_id": failed.job_id,
                "error": "measurement_failed",
                "result": failed.result,
                "retry": {
                    "method": "POST",
                    "path": f"/api/v1/runs/{name}/tools/measure",
                },
            }

        try:
            pending = await self.submit(name, "measure")
        except ToolConflict:
            pending = next(
                (
                    job
                    for job in reversed(tuple(self._jobs.values()))
                    if job.run_name == name
                    and job.tool == "measure"
                    and job.status in {"queued", "running"}
                ),
                None,
            )
            if pending is None:
                raise
        return {"status": "pending", "job_id": pending.job_id}

    async def shutdown(self) -> None:
        active_jobs = [
            job for job in self._jobs.values() if job.task and not job.task.done()
        ]
        if not active_jobs:
            return
        _signal_jobs(active_jobs, signal.SIGINT)
        pending = await _wait_jobs(active_jobs, wait_seconds=10.0)
        if pending:
            _signal_jobs(pending, signal.SIGTERM)
            pending = await _wait_jobs(pending, wait_seconds=5.0)
        if pending:
            _signal_jobs(pending, signal.SIGKILL)
            await asyncio.gather(
                *(job.task for job in pending if job.task is not None),
                return_exceptions=True,
            )

    async def _execute(self, job: ToolJob, run_root: Path) -> None:
        job.status = "running"
        job.started_at = _now()
        try:
            stdout_path = job.artifact_dir / "stdout.jsonl"
            stderr_path = job.artifact_dir / "stderr.log"
            command = [sys.executable, "-m", "terrarium.cli"]
            if job.tool in {"verify", "rebuild", "replay"}:
                command.extend((job.tool, str(run_root)))
            elif job.tool == "measure":
                config = load_manifest_config(run_root)
                config_path = job.artifact_dir / "config.snapshot.json"
                config_path.write_text(
                    config.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                output = job.artifact_dir / "measurement"
                command.extend(
                    ("measure", str(run_root), str(config_path), "--output", str(output))
                )
            else:
                output = job.artifact_dir / "viewer"
                command.extend(("viewer", str(run_root), "--output", str(output)))

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
                job.process = process
            finally:
                stdout_handle.close()
                stderr_handle.close()
            job.returncode = await process.wait()
            job.result = _parse_result(stdout_path, stderr_path, job.returncode)
            job.status = "succeeded" if job.returncode == 0 else "failed"
            if job.tool == "measure" and job.status == "succeeded":
                after = _run_identity(self.registry, job.run_name)
                if after is not None and after == job.input_identity:
                    job.measurement_identity = after
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.returncode = None
            job.result = {
                "status": "error",
                "error": "tool_dispatch_failed",
                "exception_type": type(exc).__name__,
            }
        finally:
            job.process = None
            job.ended_at = _now()
            await self.processes.release_audit(job.run_name)


def _parse_result(stdout: Path, stderr: Path, returncode: int) -> dict[str, Any]:
    source = stdout if returncode == 0 else stderr
    try:
        raw = source.read_bytes()[-256 * 1024 :]
    except FileNotFoundError:
        raw = b""
    text = sanitize_control_text(raw.decode("utf-8", errors="replace"), max_length=256 * 1024)
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {
        "status": "error",
        "error": "tool_process_failed",
        "returncode": returncode,
    }


def _run_identity(registry: RunRegistry, run_name: str) -> tuple[int, str] | None:
    record = registry.inspect(run_name)
    if record.last_seq is None or record.last_hash is None:
        return None
    return record.last_seq, record.last_hash


def _read_measurement_json(path: Path) -> list[dict[str, object]]:
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 32 * 1024 * 1024
        ):
            raise RuntimeError("measurement cache file is unsafe")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError("measurement cache JSON is invalid")
    return value


def _signal_jobs(jobs: list[ToolJob], requested: signal.Signals) -> None:
    for job in jobs:
        process = job.process
        if process is None or process.returncode is not None:
            continue
        try:
            os.killpg(process.pid, requested)
        except ProcessLookupError:
            continue


async def _wait_jobs(jobs: list[ToolJob], *, wait_seconds: float) -> list[ToolJob]:
    tasks = {job.task for job in jobs if job.task is not None and not job.task.done()}
    if not tasks:
        return []
    _done, pending = await asyncio.wait(tasks, timeout=wait_seconds)
    return [job for job in jobs if job.task in pending]


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["TOOLS", "ToolConflict", "ToolJob", "ToolManager", "ToolName"]
