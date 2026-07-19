"""FastAPI application factory for the local scientific GUI."""

from __future__ import annotations

import mimetypes
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from .api.configs import router as configs_router
from .api.runs import router as runs_router
from .api.telemetry import create_telemetry_router
from .config_store import ConfigStore
from .process_manager import ProcessManager
from .registry import RunRegistry
from .security import GuiSecurityMiddleware, require_auth
from .settings import GuiSettings
from .tools import ToolManager


def _is_spa_route(path: str) -> bool:
    normalized = path.lstrip("/")
    return not normalized.startswith("api/") and "." not in Path(normalized).name


def create_app(settings: GuiSettings | None = None) -> FastAPI:
    configured = settings or GuiSettings()

    registry = RunRegistry(configured.runs_root)
    config_store = ConfigStore(configured.configs_dir)
    processes = ProcessManager(registry, configured.artifacts_root)
    tools = ToolManager(registry, processes, configured.artifacts_root)
    telemetry_router = create_telemetry_router(
        registry.resolve, measurement_provider=tools.measurement
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # Stop run writers first; audit jobs can then finish without racing a
        # GUI-owned writer during application shutdown.
        try:
            await processes.shutdown()
        finally:
            try:
                await tools.shutdown()
            finally:
                try:
                    await telemetry_router.close_telemetry()  # type: ignore[attr-defined]
                finally:
                    config_store.close()

    app = FastAPI(
        title="AI Model Terrarium GUI",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gui_settings = configured
    app.state.config_store = config_store
    app.state.run_registry = registry
    app.state.process_manager = processes
    app.state.tool_manager = tools

    app.add_middleware(GuiSecurityMiddleware, settings=configured)
    app.include_router(configs_router)
    app.include_router(runs_router)
    app.include_router(
        telemetry_router,
        prefix="/api/v1",
        dependencies=[Depends(require_auth)],
    )

    @app.get("/api/v1/health", dependencies=[Depends(require_auth)])
    async def health(request: Request) -> dict[str, object]:
        return {
            "status": "ok",
            "runs_root": str(request.app.state.gui_settings.runs_root),
        }

    static_root = Path(__file__).with_name("static")
    if static_root.is_dir() and (static_root / "index.html").is_file():
        static_root = static_root.resolve(strict=True)

        @app.api_route("/{asset_path:path}", methods=["GET", "HEAD"])
        async def frontend(asset_path: str, request: Request) -> Response:
            selected = asset_path or "index.html"
            content = _read_static_file(static_root, selected)
            if content is None and _is_spa_route(asset_path):
                selected = "index.html"
                content = _read_static_file(static_root, selected)
            if content is None:
                raise StarletteHTTPException(status_code=404)
            media_type = mimetypes.guess_type(selected)[0] or "application/octet-stream"
            headers = {
                "Cache-Control": (
                    "public, max-age=31536000, immutable"
                    if selected.startswith("assets/")
                    else "no-cache"
                )
            }
            return Response(
                content=b"" if request.method == "HEAD" else content,
                media_type=media_type,
                headers=headers,
            )
    else:

        @app.get("/")
        async def frontend_missing() -> JSONResponse:
            return JSONResponse(
                {
                    "status": "frontend_not_built",
                    "detail": "Run npm install && npm run build in gui/frontend.",
                },
                status_code=503,
            )

    return app


def _read_static_file(root: Path, relative: str) -> bytes | None:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    candidate = root.joinpath(*path.parts)
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_relative_to(root)
        or info.st_size > 16 * 1024 * 1024
    ):
        return None
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(resolved, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != info.st_size
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            return None
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


__all__ = ["create_app"]
