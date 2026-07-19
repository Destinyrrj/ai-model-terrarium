"""Local-server request authentication and browser hardening."""

from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable, MutableMapping
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi import HTTPException, Request

from .settings import GuiSettings

_STREAM_PATH = re.compile(r"^/api/v1/runs/[A-Za-z0-9][A-Za-z0-9_-]{0,95}/stream$")
_AUTH_STATE_KEY = "terrarium_gui_stream_tokens"
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    # ECharts and React set numeric layout attributes on trusted, code-created
    # elements.  Keep style elements external while allowing those attributes.
    "style-src-attr 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", _CSP.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)


class GuiSecurityMiddleware:
    """Validate Host, scrub SSE query credentials, and add security headers.

    Browsers' native ``EventSource`` cannot attach an Authorization header.  A
    query credential is therefore recognized only on the exact run-stream
    route.  It is removed from ``scope['query_string']`` before downstream
    handling so uvicorn's response-time access log cannot reproduce it.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], settings: GuiSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[..., Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = _request_host(scope)
        if host not in self.settings.allowed_hosts:
            await _plain_error(send, HTTPStatus.BAD_REQUEST, "invalid Host header")
            return

        if scope["type"] == "http":
            _extract_and_scrub_stream_token(scope)

        async def secure_send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value) for name, value in _SECURITY_HEADERS if name not in present
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)


async def require_auth(request: Request) -> None:
    """FastAPI dependency requiring this GUI session's bearer credential."""

    settings = getattr(request.app.state, "gui_settings", None)
    if not isinstance(settings, GuiSettings):
        # A missing server-side setting is an application error, not an auth
        # oracle.  Keep the client-facing failure generic.
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE, detail="GUI unavailable")

    candidate = _authorization_token(request.headers.get("authorization"))
    if candidate is None and _STREAM_PATH.fullmatch(request.url.path):
        state = request.scope.get("state", {})
        stream_tokens = state.pop(_AUTH_STATE_KEY, ())
        if len(stream_tokens) == 1:
            candidate = stream_tokens[0]

    if candidate is None or not hmac.compare_digest(candidate, settings.access_token):
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _authorization_token(header: str | None) -> str | None:
    if header is None:
        return None
    scheme, separator, credentials = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not credentials:
        return None
    if credentials != credentials.strip() or any(character.isspace() for character in credentials):
        return None
    return credentials


def _request_host(scope: MutableMapping[str, Any]) -> str | None:
    raw_values = [
        value
        for name, value in scope.get("headers", ())
        if bytes(name).lower() == b"host"
    ]
    if len(raw_values) != 1:
        return None
    try:
        authority = bytes(raw_values[0]).decode("ascii")
    except UnicodeDecodeError:
        return None

    if authority.startswith("["):
        # The server is IPv4-loopback-only, so bracketed IPv6 is never valid.
        return None
    hostname, separator, port = authority.rpartition(":")
    if separator:
        if not hostname or not port.isascii() or not port.isdigit():
            return None
        if not 0 < int(port) <= 65535:
            return None
    else:
        hostname = authority
    if not hostname or hostname.endswith("."):
        return None
    return hostname.lower()


def _extract_and_scrub_stream_token(scope: MutableMapping[str, Any]) -> None:
    if scope.get("method") != "GET" or not _STREAM_PATH.fullmatch(str(scope.get("path", ""))):
        return
    raw_query = bytes(scope.get("query_string", b""))
    try:
        pairs = parse_qsl(
            raw_query.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=128,
        )
    except (UnicodeDecodeError, ValueError):
        return

    credentials = tuple(value for key, value in pairs if key == "access_token")
    if not credentials:
        return
    safe_pairs = [(key, value) for key, value in pairs if key != "access_token"]
    scope["query_string"] = urlencode(safe_pairs, doseq=True).encode("ascii")
    state = scope.setdefault("state", {})
    state[_AUTH_STATE_KEY] = credentials


async def _plain_error(
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    status: HTTPStatus,
    detail: str,
) -> None:
    body = detail.encode("utf-8")
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        *_SECURITY_HEADERS,
    ]
    await send({"type": "http.response.start", "status": int(status), "headers": headers})
    await send({"type": "http.response.body", "body": body})


__all__ = ["GuiSecurityMiddleware", "require_auth"]
