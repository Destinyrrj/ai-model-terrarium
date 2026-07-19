from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from terrarium_gui.app import create_app
from terrarium_gui.settings import GuiSettings


@pytest.mark.asyncio
async def test_spa_deep_links_fallback_but_unknown_api_stays_404(tmp_path: Path) -> None:
    token = "static-integration-token-1234"  # noqa: S105 - inert test credential
    app = create_app(
        GuiSettings(
            runs_root=tmp_path / "runs",
            configs_dir=tmp_path / "configs",
            artifacts_root=tmp_path / "artifacts",
            dev_token=SecretStr(token),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        deep_link = await client.get("/runs/example")
        missing_api = await client.get("/api/v1/not-a-route")

    assert deep_link.status_code == 200
    assert '<div id="root"></div>' in deep_link.text
    assert missing_api.status_code == 404
    assert "text/html" not in missing_api.headers.get("content-type", "")
