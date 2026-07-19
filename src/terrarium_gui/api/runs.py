"""Run lifecycle and audit-job HTTP endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from terrarium.replay import ReplayError

from ..config_store import (
    ConfigNotFound,
    ConfigValidationFailure,
    InvalidConfigName,
    UnsafeConfigFile,
)
from ..process_manager import ManagedProcessNotFound, ProcessConflict, ProcessManager
from ..registry import InvalidRunName, RunNotFound, RunRegistry, validate_run_name
from ..security import require_auth
from ..tools import TOOLS, ToolConflict, ToolManager

router = APIRouter(
    prefix="/api/v1", tags=["runs"], dependencies=[Depends(require_auth)]
)
MAX_TICKS = (1 << 63) - 1


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    config_name: str = Field(min_length=1, max_length=96)
    run_name: str = Field(min_length=1, max_length=96)
    max_ticks: int | None = Field(default=None, ge=0, le=MAX_TICKS)


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    max_ticks: int | None = Field(default=None, ge=0, le=MAX_TICKS)


def _services(request: Request) -> tuple[RunRegistry, ProcessManager, ToolManager]:
    return (
        request.app.state.run_registry,
        request.app.state.process_manager,
        request.app.state.tool_manager,
    )


@router.get("/runs")
async def list_runs(request: Request) -> dict[str, object]:
    registry, processes, _ = _services(request)
    records = registry.list(managed_running=processes.running_names())
    items = [record.to_dict() for record in records]
    discovered = {str(item["name"]) for item in items}
    for name in sorted(processes.known_names() - discovered):
        handle = processes.get_for_run(name)
        if handle is not None:
            items.append(_synthetic_run(handle.public()))
    return {"runs": items}


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(body: StartRunRequest, request: Request) -> dict[str, object]:
    _, processes, _ = _services(request)
    try:
        validate_run_name(body.run_name)
        stored = request.app.state.config_store.get(body.config_name)
        config = request.app.state.config_store.validate(stored.text)
        handle = await processes.start(
            run_name=body.run_name,
            config=config,
            config_bytes=stored.text.encode("utf-8"),
            max_ticks=body.max_ticks,
        )
    except (FileNotFoundError, ConfigNotFound) as exc:
        raise HTTPException(status_code=404, detail="config_not_found") from exc
    except (
        ConfigValidationFailure,
        InvalidConfigName,
        InvalidRunName,
        UnsafeConfigFile,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=422, detail="invalid_run_request") from exc
    except ProcessConflict as exc:
        raise HTTPException(status_code=409, detail="run_conflict") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="process_start_failed") from exc
    return {"status": "accepted", "job": handle.public()}


@router.get("/runs/{run_name}")
async def get_run(run_name: str, request: Request) -> dict[str, object]:
    registry, processes, _ = _services(request)
    try:
        record = registry.inspect(run_name, managed_running=processes.is_running(run_name))
    except InvalidRunName as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    except RunNotFound as exc:
        process = processes.get_for_run(run_name)
        if process is not None:
            return {"run": _synthetic_run(process.public()), "process": process.public()}
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    except ReplayError as exc:
        raise HTTPException(status_code=409, detail="run_not_resumable") from exc
    process = processes.get_for_run(run_name)
    return {"run": record.to_dict(), "process": process.public() if process else None}


@router.post("/runs/{run_name}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_run(run_name: str, request: Request) -> dict[str, object]:
    _, processes, _ = _services(request)
    try:
        handle = await processes.stop(run_name)
    except InvalidRunName as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    except ManagedProcessNotFound as exc:
        raise HTTPException(status_code=409, detail="run_not_managed_or_stopped") from exc
    return {"status": "stopped", "job": handle.public()}


@router.post("/runs/{run_name}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_run(
    run_name: str, body: ResumeRunRequest, request: Request
) -> dict[str, object]:
    _, processes, _ = _services(request)
    try:
        handle = await processes.resume(run_name, max_ticks=body.max_ticks)
    except (InvalidRunName, RunNotFound) as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    except ReplayError as exc:
        raise HTTPException(status_code=409, detail="run_not_resumable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_run_request") from exc
    except ProcessConflict as exc:
        raise HTTPException(status_code=409, detail="run_conflict") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="process_start_failed") from exc
    return {"status": "accepted", "job": handle.public()}


@router.get("/runs/{run_name}/stderr")
async def run_stderr(
    run_name: str,
    request: Request,
    max_bytes: Annotated[int, Query(ge=1, le=256 * 1024)] = 64 * 1024,
) -> dict[str, object]:
    _, processes, _ = _services(request)
    try:
        text = processes.stderr_tail(run_name, max_bytes=max_bytes)
    except InvalidRunName as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    return {"run_name": run_name, "stderr": text, "truncated_to_bytes": max_bytes}


@router.get("/jobs/{job_id}")
async def get_process_job(job_id: str, request: Request) -> dict[str, object]:
    _, processes, _ = _services(request)
    handle = processes.get(job_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {"job": handle.public()}


@router.post(
    "/runs/{run_name}/tools/{tool}",
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_tool(run_name: str, tool: str, request: Request) -> dict[str, object]:
    if tool not in TOOLS:
        raise HTTPException(status_code=404, detail="tool_not_found")
    _, _, tools = _services(request)
    try:
        job = await tools.submit(run_name, tool)
    except (InvalidRunName, RunNotFound) as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    except ToolConflict as exc:
        raise HTTPException(status_code=409, detail="run_active_or_tool_busy") from exc
    return {"status": "accepted", "job": job.public()}


@router.get("/tool-jobs/{job_id}")
async def get_tool_job(job_id: str, request: Request) -> dict[str, object]:
    _, _, tools = _services(request)
    job = tools.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {"job": job.public()}


@router.get("/runs/{run_name}/tool-jobs")
async def list_tool_jobs(run_name: str, request: Request) -> dict[str, object]:
    _, _, tools = _services(request)
    try:
        jobs = tools.list_for_run(run_name)
    except InvalidRunName as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    return {"jobs": [job.public() for job in jobs]}


def _synthetic_run(process: dict[str, object]) -> dict[str, object]:
    running = process.get("running") is True
    return {
        "name": process["run_name"],
        "status": "initializing" if running else "failed",
        "run_id": None,
        "last_tick": None,
        "last_seq": None,
        "target_generation": None,
        "current_generation": None,
        "completed": False,
        "has_manifest": False,
        "has_events": False,
        "has_projection": False,
        "error": None if running else "run_directory_missing",
    }


__all__ = ["router"]
