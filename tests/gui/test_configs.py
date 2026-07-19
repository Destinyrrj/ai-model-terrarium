from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from terrarium_gui.api.configs import router
from terrarium_gui.config_store import (
    ConfigStore,
    ConfigValidationFailure,
    InvalidConfigName,
    UnsafeConfigFile,
)
from terrarium_gui.security import GuiSecurityMiddleware
from terrarium_gui.settings import GuiSettings

TOKEN = "local-test-token-with-32-bytes-ok"  # noqa: S105 - inert test credential


def _app(tmp_path: Path) -> FastAPI:
    settings = GuiSettings(
        runs_root=tmp_path / "runs",
        configs_dir=tmp_path / "configs",
        artifacts_root=tmp_path / "artifacts",
        dev_token=TOKEN,
    )
    app = FastAPI()
    app.state.gui_settings = settings
    app.state.config_store = ConfigStore(settings.configs_dir)
    app.add_middleware(GuiSecurityMiddleware, settings=settings)
    app.include_router(router)
    return app


@pytest.fixture
def mvp_yaml() -> str:
    return Path("configs/mvp.yaml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_config_schema_and_crud_are_authenticated(
    tmp_path: Path, mvp_yaml: str
) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        schema = await client.get("/api/v1/schema/config")
        assert schema.status_code == 200
        assert "world" in schema.json()["properties"]
        assert "default-src 'none'" in schema.headers["content-security-policy"]

        written = await client.put(
            "/api/v1/configs/demo",
            content=mvp_yaml,
            headers={"Content-Type": "text/yaml"},
        )
        assert written.status_code == 200
        assert len(written.json()["digest"]) == 64

        listed = await client.get("/api/v1/configs")
        assert [item["name"] for item in listed.json()["configs"]] == ["demo"]

        fetched = await client.get("/api/v1/configs/demo")
        assert fetched.status_code == 200
        assert fetched.json()["yaml"] == mvp_yaml

        deleted = await client.delete("/api/v1/configs/demo")
        assert deleted.status_code == 204
        assert not (tmp_path / "configs" / "demo.yaml").exists()

    unauthenticated = httpx.ASGITransport(app=_app(tmp_path / "other"))
    async with httpx.AsyncClient(
        transport=unauthenticated, base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/api/v1/schema/config")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_validation_reports_pydantic_locations(tmp_path: Path, mvp_yaml: str) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        response = await client.post(
            "/api/v1/configs/validate",
            content=mvp_yaml.replace("rain_probability: 0.18", "rain_probability: 2.0"),
            headers={"Content-Type": "application/yaml"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_config"
    assert detail["valid"] is False
    assert detail["errors"][0]["loc"] == ["world", "rain_probability"]
    assert "input" not in detail["errors"][0]


def test_store_rejects_paths_symlinks_and_invalid_documents(
    tmp_path: Path, mvp_yaml: str
) -> None:
    store = ConfigStore(tmp_path / "configs")
    with pytest.raises(InvalidConfigName):
        store.put("../escape", mvp_yaml)

    outside = tmp_path / "outside.yaml"
    outside.write_text(mvp_yaml, encoding="utf-8")
    (store.root / "linked.yaml").symlink_to(outside)
    with pytest.raises(UnsafeConfigFile):
        store.get("linked")

    with pytest.raises(ConfigValidationFailure) as raised:
        store.validate("schema_version: 1\nschema_version: 1\n")
    assert raised.value.issues[0].type == "yaml_parse"


def test_atomic_write_leaves_no_temporary_files(tmp_path: Path, mvp_yaml: str) -> None:
    store = ConfigStore(tmp_path / "configs")
    store.put("stable", mvp_yaml)
    store.put("stable", mvp_yaml.replace("seed: 731993", "seed: 4"))

    assert store.get("stable").text.count("seed: 4") == 1
    assert [path.name for path in store.root.iterdir()] == ["stable.yaml"]


def test_root_swap_cannot_escape_retained_directory_fd(
    tmp_path: Path, mvp_yaml: str
) -> None:
    root = tmp_path / "configs"
    store = ConfigStore(root)
    store.put("secret", mvp_yaml)
    original = tmp_path / "configs-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.yaml").write_text("outside: true\n", encoding="utf-8")

    root.rename(original)
    root.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(UnsafeConfigFile, match="identity changed"):
            store.get("secret")
    finally:
        root.unlink()
        original.rename(root)
        store.close()
