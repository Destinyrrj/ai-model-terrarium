from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from terrarium_gui.tools import ToolJob, ToolManager


class _Registry:
    def inspect(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(last_seq=12, last_hash="a" * 64)


@pytest.mark.asyncio
async def test_failed_measurement_is_stable_until_explicit_retry(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manager = ToolManager(_Registry(), object(), artifacts)  # type: ignore[arg-type]
    artifact_dir = manager.root / "failed-job"
    artifact_dir.mkdir()
    failed = ToolJob(
        job_id="failed-job",
        run_name="demo",
        tool="measure",
        status="failed",
        created_at="2026-07-19T00:00:00+00:00",
        artifact_dir=artifact_dir,
        input_identity=(12, "a" * 64),
        result={"status": "error", "error": "tool_process_failed"},
    )
    manager._jobs[failed.job_id] = failed

    first = await manager.measurement("demo", "behavior")
    second = await manager.measurement("demo", "knowledge-survival")

    assert first["status"] == second["status"] == "failed"
    assert first["job_id"] == second["job_id"] == "failed-job"
    assert first["retry"] == {
        "method": "POST",
        "path": "/api/v1/runs/demo/tools/measure",
    }
    assert list(manager._jobs) == ["failed-job"]


@pytest.mark.asyncio
async def test_missing_success_artifact_becomes_stable_retryable_failure(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manager = ToolManager(_Registry(), object(), artifacts)  # type: ignore[arg-type]
    artifact_dir = manager.root / "missing-cache"
    artifact_dir.mkdir()
    job = ToolJob(
        job_id="missing-cache",
        run_name="demo",
        tool="measure",
        status="succeeded",
        created_at="2026-07-19T00:00:00+00:00",
        artifact_dir=artifact_dir,
        input_identity=(12, "a" * 64),
        measurement_identity=(12, "a" * 64),
    )
    manager._jobs[job.job_id] = job

    result = await manager.measurement("demo", "behavior")

    assert result["status"] == "failed"
    assert result["error"] == "measurement_cache_invalid"
    assert job.status == "failed"
    assert job.measurement_identity is None
