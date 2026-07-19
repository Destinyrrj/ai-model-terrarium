"""Configuration schema, validation, and CRUD endpoints."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from terrarium.config import MAX_CONFIG_BYTES, RunConfig

from ..config_store import (
    ConfigNotFound,
    ConfigStore,
    ConfigTooLarge,
    ConfigValidationFailure,
    InvalidConfigName,
    UnsafeConfigFile,
)
from ..schemas import (
    ConfigDocument,
    ConfigList,
    ConfigValidationResult,
    ConfigWriteResult,
    StoredConfig,
)
from ..security import require_auth

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])


async def _store(request: Request) -> ConfigStore:
    store = getattr(request.app.state, "config_store", None)
    if not isinstance(store, ConfigStore):
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail="GUI unavailable")
    return store


Store = Annotated[ConfigStore, Depends(_store)]


@router.get("/schema/config")
async def config_schema() -> dict[str, Any]:
    """Return the authoritative Pydantic schema driving the config form."""

    return RunConfig.model_json_schema(mode="validation")


@router.get("/configs", response_model=ConfigList)
async def list_configs(store: Store) -> ConfigList:
    try:
        return ConfigList(configs=store.list())
    except UnsafeConfigFile as exc:
        raise _http_error(HTTPStatus.CONFLICT, "unsafe_config_store", str(exc)) from exc


@router.get("/configs/{name}", response_model=StoredConfig)
async def get_config(name: str, store: Store) -> StoredConfig:
    try:
        document = store.get(name)
    except InvalidConfigName as exc:
        raise _http_error(HTTPStatus.BAD_REQUEST, "invalid_config_name", str(exc)) from exc
    except ConfigNotFound as exc:
        raise _http_error(HTTPStatus.NOT_FOUND, "config_not_found", str(exc)) from exc
    except (UnsafeConfigFile, ConfigTooLarge) as exc:
        raise _http_error(HTTPStatus.CONFLICT, "unsafe_config_file", str(exc)) from exc
    return StoredConfig(**document.summary.model_dump(), yaml=document.text)


@router.put("/configs/{name}", response_model=ConfigWriteResult)
async def put_config(name: str, request: Request, store: Store) -> ConfigWriteResult:
    text = await _yaml_body(request)
    try:
        summary, config = store.put(name, text)
    except InvalidConfigName as exc:
        raise _http_error(HTTPStatus.BAD_REQUEST, "invalid_config_name", str(exc)) from exc
    except ConfigValidationFailure as exc:
        raise _validation_error(exc) from exc
    except ConfigTooLarge as exc:
        raise _http_error(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "config_too_large", str(exc)
        ) from exc
    except UnsafeConfigFile as exc:
        raise _http_error(HTTPStatus.CONFLICT, "unsafe_config_file", str(exc)) from exc
    return ConfigWriteResult(**summary.model_dump(), digest=config.digest())


@router.delete("/configs/{name}", status_code=HTTPStatus.NO_CONTENT)
async def delete_config(name: str, store: Store) -> Response:
    try:
        store.delete(name)
    except InvalidConfigName as exc:
        raise _http_error(HTTPStatus.BAD_REQUEST, "invalid_config_name", str(exc)) from exc
    except ConfigNotFound as exc:
        raise _http_error(HTTPStatus.NOT_FOUND, "config_not_found", str(exc)) from exc
    except UnsafeConfigFile as exc:
        raise _http_error(HTTPStatus.CONFLICT, "unsafe_config_file", str(exc)) from exc
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post("/configs/validate", response_model=ConfigValidationResult)
async def validate_config(request: Request, store: Store) -> ConfigValidationResult:
    text = await _yaml_body(request)
    try:
        config = store.validate(text)
    except ConfigValidationFailure as exc:
        raise _validation_error(exc) from exc
    except ConfigTooLarge as exc:
        raise _http_error(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "config_too_large", str(exc)
        ) from exc
    return ConfigValidationResult(
        valid=True,
        digest=config.digest(),
        config=config.model_dump(mode="json"),
    )


async def _yaml_body(request: Request) -> str:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise _http_error(
                HTTPStatus.BAD_REQUEST, "invalid_content_length", "invalid Content-Length"
            ) from exc
        if declared < 0 or declared > MAX_CONFIG_BYTES + 1024:
            raise _http_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "config_too_large",
                f"configuration exceeds {MAX_CONFIG_BYTES} bytes",
            )

    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_CONFIG_BYTES + 1024:
            raise _http_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "config_too_large",
                f"configuration exceeds {MAX_CONFIG_BYTES} bytes",
            )
        chunks.append(chunk)
    body = b"".join(chunks)

    content_type = request.headers.get("content-type", "text/yaml").partition(";")[0].strip()
    if content_type in {"application/json", "application/problem+json"}:
        try:
            payload = ConfigDocument.model_validate_json(body)
        except ValidationError as exc:
            issues = [
                {
                    "loc": list(error["loc"]),
                    "msg": str(error["msg"]),
                    "type": str(error["type"]),
                }
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_request", "errors": issues},
            ) from exc
        return payload.yaml

    if content_type not in {"text/yaml", "application/yaml", "text/plain", ""}:
        raise _http_error(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "send YAML text or application/json with a yaml field",
        )
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _http_error(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "invalid_encoding",
            "configuration must be valid UTF-8",
        ) from exc


def _validation_error(exc: ConfigValidationFailure) -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        detail={
            "code": "invalid_config",
            "valid": False,
            "errors": [issue.model_dump(mode="json") for issue in exc.issues],
        },
    )


def _http_error(status: HTTPStatus, code: str, message: str) -> HTTPException:
    # JSON round-tripping guarantees the payload contains only ordinary JSON
    # primitives even when exception strings originated in a parser.
    safe_message = json.loads(json.dumps(message, ensure_ascii=False))
    return HTTPException(status_code=status, detail={"code": code, "message": safe_message})


__all__ = ["router"]
