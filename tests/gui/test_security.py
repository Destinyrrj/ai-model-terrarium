from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from pydantic import ValidationError

from terrarium_gui.security import GuiSecurityMiddleware, require_auth
from terrarium_gui.settings import GuiSettings

TOKEN = "event-source-test-token-32-bytes"  # noqa: S105 - inert test credential


def _settings(tmp_path: Path) -> GuiSettings:
    return GuiSettings(
        runs_root=tmp_path / "runs",
        configs_dir=tmp_path / "configs",
        artifacts_root=tmp_path / "artifacts",
        dev_token=TOKEN,
    )


def _app(tmp_path: Path) -> FastAPI:
    settings = _settings(tmp_path)
    app = FastAPI()
    app.state.gui_settings = settings
    app.add_middleware(GuiSecurityMiddleware, settings=settings)

    @app.get("/api/v1/protected", dependencies=[Depends(require_auth)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/runs/demo/stream", dependencies=[Depends(require_auth)])
    async def stream(request: Request) -> dict[str, object]:
        return {"query": list(request.query_params.multi_items())}

    return app


@pytest.mark.asyncio
async def test_query_token_is_stream_only_and_scrubbed(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        stream = await client.get(
            "/api/v1/runs/demo/stream",
            params={"access_token": TOKEN, "cursor": "8"},
        )
        elsewhere = await client.get(
            "/api/v1/protected", params={"access_token": TOKEN}
        )

    assert stream.status_code == 200
    assert stream.json() == {"query": [["cursor", "8"]]}
    assert elsewhere.status_code == 401


@pytest.mark.asyncio
async def test_host_check_rejects_dns_rebinding_names(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://attacker.example",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        response = await client.get("/api/v1/protected")

    assert response.status_code == 400
    assert response.text == "invalid Host header"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_settings_are_loopback_only_and_tokens_are_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.host == "127.0.0.1"
    assert TOKEN not in repr(settings)
    with pytest.raises(ValidationError):
        GuiSettings(
            runs_root=tmp_path / "runs",
            configs_dir=tmp_path / "configs",
            artifacts_root=tmp_path / "artifacts",
            host="0.0.0.0",  # type: ignore[arg-type]  # noqa: S104
        )
    with pytest.raises(ValidationError):
        GuiSettings(
            runs_root=tmp_path / "runs",
            configs_dir=tmp_path / "configs",
            artifacts_root=tmp_path,
        )
    with pytest.raises(ValidationError):
        GuiSettings(
            runs_root=tmp_path / "runs-2",
            configs_dir=tmp_path / "configs-2",
            artifacts_root=tmp_path / "artifacts-2",
            dev_token="x" * 24 + "\x1b",
        )
