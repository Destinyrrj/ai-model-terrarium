from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from terrarium.storage import EventStore
from terrarium_gui.app import create_app
from terrarium_gui.settings import GuiSettings

TOKEN = "integration-test-token-123456"  # noqa: S105 - inert test credential
MVP_YAML = Path("configs/mvp.yaml").read_text(encoding="utf-8")


async def _wait_process(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(200):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()["job"]
        if not job["running"]:
            return job
        await asyncio.sleep(0.025)
    raise AssertionError("managed process did not finish")


async def _wait_tool(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(200):
        response = await client.get(f"/api/v1/tool-jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()["job"]
        if job["status"] not in {"queued", "running"}:
            return job
        await asyncio.sleep(0.025)
    raise AssertionError("audit tool did not finish")


@pytest.mark.asyncio
async def test_api_run_resume_telemetry_and_audit(tmp_path: Path) -> None:
    settings = GuiSettings(
        runs_root=tmp_path / "runs",
        configs_dir=tmp_path / "configs",
        artifacts_root=tmp_path / "artifacts",
        dev_token=SecretStr(TOKEN),
    )
    app = create_app(settings)
    source = MVP_YAML
    app.state.config_store.put("mvp", source)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as client:
        started = await client.post(
            "/api/v1/runs",
            json={"config_name": "mvp", "run_name": "api-demo", "max_ticks": 0},
        )
        assert started.status_code == 202
        first = await _wait_process(client, started.json()["job"]["job_id"])
        assert first["returncode"] == 0

        run = await client.get("/api/v1/runs/api-demo")
        assert run.status_code == 200
        assert run.json()["run"]["status"] == "resumable"
        assert run.json()["run"]["last_tick"] == 0

        resumed = await client.post(
            "/api/v1/runs/api-demo/resume", json={"max_ticks": 1}
        )
        assert resumed.status_code == 202
        second = await _wait_process(client, resumed.json()["job"]["job_id"])
        assert second["returncode"] == 0

        ticks = await client.get("/api/v1/runs/api-demo/ticks")
        assert ticks.status_code == 200
        assert ticks.json()["ticks"][-1]["tick"] == 1
        budget = await client.get("/api/v1/runs/api-demo/metrics/budget")
        assert budget.status_code == 200
        assert len(budget.json()["gauges"]) == 4

        measurement = await client.get(
            "/api/v1/runs/api-demo/metrics/knowledge-survival"
        )
        assert measurement.status_code == 202
        measure_job = await _wait_tool(client, measurement.json()["job_id"])
        assert measure_job["status"] == "succeeded"
        cached = await client.get(
            "/api/v1/runs/api-demo/metrics/knowledge-survival"
        )
        assert cached.status_code == 200
        assert cached.json()["status"] == "ready"
        assert cached.json()["points"]

        verified = await client.post("/api/v1/runs/api-demo/tools/verify")
        assert verified.status_code == 202
        audit = await _wait_tool(client, verified.json()["job"]["job_id"])
        assert audit["status"] == "succeeded"
        assert audit["result"]["status"] == "ok"

        # Holding the real sealed writer lock must make audit dispatch fail
        # before a competing CLI is spawned.
        config = app.state.config_store.validate(source)
        with EventStore(
            settings.runs_root / "api-demo",
            config.run_id,
            max_raw_bytes=max(1, config.storage.max_raw_bytes),
        ):
            conflict = await client.post("/api/v1/runs/api-demo/tools/replay")
        assert conflict.status_code == 409

    await app.state.process_manager.shutdown()
    await app.state.tool_manager.shutdown()

    run_files = {path.name for path in (settings.runs_root / "api-demo").iterdir()}
    assert "config.snapshot.json" not in run_files
    assert (settings.artifacts_root / "runs" / "api-demo" / "config.snapshot.json").is_file()


@pytest.mark.asyncio
async def test_run_api_requires_bearer_token(tmp_path: Path) -> None:
    settings = GuiSettings(
        runs_root=tmp_path / "runs",
        configs_dir=tmp_path / "configs",
        artifacts_root=tmp_path / "artifacts",
        dev_token=SecretStr(TOKEN),
    )
    app = create_app(settings)
    corrupt = settings.runs_root / "corrupt"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{}\n", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/v1/runs")).status_code == 401
        assert (await client.get("/api/v1/runs/nope/events")).status_code == 401

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        response = await client.post("/api/v1/runs/corrupt/resume", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == "run_not_resumable"


@pytest.mark.asyncio
async def test_oversized_integer_cursors_are_rejected_before_sqlite(
    tmp_path: Path,
) -> None:
    settings = GuiSettings(
        runs_root=tmp_path / "runs",
        configs_dir=tmp_path / "configs",
        artifacts_root=tmp_path / "artifacts",
        dev_token=SecretStr(TOKEN),
    )
    app = create_app(settings)
    huge = str(10**100)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as client:
        paths = (
            f"/api/v1/runs/demo/events?after_seq={huge}",
            f"/api/v1/runs/demo/agents?offset={huge}",
            f"/api/v1/runs/demo/world?tick={huge}",
            f"/api/v1/runs/demo/metrics/tokens?after_tick={huge}",
        )
        for path in paths:
            assert (await client.get(path)).status_code == 422
        assert (
            await client.post(
                "/api/v1/runs",
                json={"config_name": "mvp", "run_name": "demo", "max_ticks": 10**100},
            )
        ).status_code == 422
