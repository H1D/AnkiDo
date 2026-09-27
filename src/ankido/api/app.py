# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Route

from ankido import __version__, oauth
from ankido.api import oauth as oauth_api
from ankido.api import shim, v1
from ankido.api.deps import client_ip
from ankido.api.mcp import McpEndpoint
from ankido.config import Config
from ankido.errors import ApiError, InternalError, PayloadTooLarge
from ankido.logging import get_logger
from ankido.supervisor import Supervisor

log = get_logger("ankido.http")


def create_app(config: Config, supervisor: Supervisor | None = None) -> FastAPI:
    sup = supervisor or Supervisor(config)
    mcp = McpEndpoint(sup)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        sup.start()
        status = oauth.oauth_status(config)
        if status != "ok":
            log.warning(
                "MCP OAuth unavailable (claude.ai connectors cannot sign in); static tokens work",
                fields={
                    "oauth": status,
                    "fix": "set server.public_url to the external https origin"
                    if status == "not_configured"
                    else "server.public_url must be https",
                },
            )
        try:
            async with mcp.lifespan():
                yield
        finally:
            sup.stop()

    app = FastAPI(
        title="Ankido",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.supervisor = sup
    app.state.config = config
    app.state.oauth_clients = oauth.ClientRegistry(sup.store)
    app.state.oauth_pending = oauth.PendingStore()

    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "If-None-Match"],
            expose_headers=["ETag"],
        )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None and not length.isdigit():
            return v1.error_response(ApiError("invalid Content-Length", code="bad_request"))
        if length and int(length) > config.server.max_body_bytes:
            exc = PayloadTooLarge(f"body exceeds {config.server.max_body_bytes} bytes")
            return v1.error_response(exc)
        if length is None and request.method in ("POST", "PUT", "PATCH"):
            # Chunked uploads cannot be size-checked up front; require a length.
            exc = ApiError("Content-Length is required", code="length_required", status=411)
            return v1.error_response(exc)
        started = time.monotonic()
        route = "unmatched"
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", fields={"path": request.url.path})
            response = v1.error_response(InternalError("internal error"))
        elapsed = time.monotonic() - started
        route_obj = request.scope.get("route")
        if route_obj is not None:
            route = getattr(route_obj, "path", route)
        sup.metrics.inc("http_requests_total", route=route, status=str(response.status_code))
        sup.metrics.observe("http_request_seconds", elapsed, route=route)
        client = client_ip(request)
        log.info(
            "request",
            fields={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": int(elapsed * 1000),
                "client": client,
                "token_id": getattr(request.state, "token_id", None),
            },
        )
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return v1.error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        err = ApiError(
            "request validation failed",
            code="validation_error",
            status=422,
            details={"errors": _clean_errors(list(exc.errors()))},
        )
        return v1.error_response(err)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(v1.router)
    app.include_router(shim.router)
    app.include_router(oauth_api.router)
    # Plain routes rather than a mount: the endpoint is exactly /mcp/p/{profile}, no redirect.
    for path in ("/mcp/p/{profile}", "/mcp/p/{profile}/"):
        app.router.routes.append(Route(path, mcp, methods=["GET", "POST", "DELETE"]))
    return app


def _clean_errors(errors: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in errors:
        out.append({"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")})
    return out
