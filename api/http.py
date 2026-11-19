"""Secure aiohttp REST v1 and GraphQL server for native, source and Docker.

The HTTP layer never invokes Telegram handlers or shell commands. Every route
uses one registered, scoped application operation and the same AppContext
service clients as the operator console. No network listener starts unless
explicitly requested by the user.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from contextlib import suppress
import hmac
import ipaddress
import json
import logging
import time
from typing import Any, Mapping
import uuid

from aiohttp import web

from api.graphql import execute_graphql
from api.jobs import JobManager, LONG_OPERATIONS
from api.openapi import openapi_document
from api.operations import ApiOperationError, OPERATIONS, execute_operation
from core.settings import ApiSettings, AppSettings, get_settings
from services.runtime.app_context import AppContext

_LOG = logging.getLogger("api.http")


def _envelope(request: web.Request, data: Any) -> web.Response:
    return web.json_response({
        "data": data,
        "meta": {
            "version": "v1",
            "request_id": request["request_id"],
        },
    })


def _error(request: web.Request, status: int, message: str) -> web.Response:
    return web.json_response(
        {"error": {
            "code": status, "message": message,
            "request_id": request.get("request_id", ""),
        }},
        status=status,
    )


class _WindowLimiter:
    """Bounded in-memory per-peer quota; never stores credentials or queries."""

    def __init__(self, limit: int, *, max_peers: int = 2048) -> None:
        self.limit = limit
        self.max_peers = max_peers
        self._buckets: dict[str, deque[float]] = {}

    def allow(self, peer: str) -> bool:
        now = time.monotonic()
        queue = self._buckets.get(peer)
        if queue is None:
            if len(self._buckets) >= self.max_peers:
                oldest = next(iter(self._buckets))
                self._buckets.pop(oldest, None)
            queue = deque()
            self._buckets[peer] = queue
        while queue and queue[0] <= now - 60.0:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True


def _is_loopback(peer: str | None) -> bool:
    if peer is None:
        return False
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _role(request: web.Request, settings: ApiSettings) -> str | None:
    """Authenticate direct peer; never trust X-Forwarded-For for local mode."""
    if settings.auth_mode == "local":
        return "admin" if _is_loopback(request.remote) else None
    header = request.headers.get("Authorization", "")
    if settings.auth_mode == "basic":
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            return None
        if (
            hmac.compare_digest(username, settings.basic_username)
            and hmac.compare_digest(password, settings.basic_password)
        ):
            return "admin"
        return None
    token = request.headers.get("X-API-Key", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        return None
    # Constant-time comparison, including the less-privileged read key.
    if settings.admin_key and hmac.compare_digest(token, settings.admin_key):
        return "admin"
    if settings.read_key and hmac.compare_digest(token, settings.read_key):
        return "read"
    return None


def _allowed_origin(request: web.Request, settings: ApiSettings) -> str | None:
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin and origin in settings.cors_origins:
        return origin
    return None


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.content_type == "application/json":
        raise ApiOperationError(415, "Expected Content-Type: application/json")
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise ApiOperationError(400, "Invalid JSON request") from None
    if not isinstance(data, dict):
        raise ApiOperationError(400, "JSON body must be an object")
    return data


@web.middleware
async def guard(request: web.Request, handler: Any) -> web.Response:
    settings: ApiSettings = request.app["api_settings"]
    request["request_id"] = uuid.uuid4().hex
    started = time.monotonic()
    role = "public"
    status = 500
    origin = request.headers.get("Origin", "")
    cors_origin = _allowed_origin(request, settings)

    try:
        peer = request.remote or "unknown"
        limiter: _WindowLimiter = request.app["api_limiter"]
        if not limiter.allow(peer):
            raise ApiOperationError(429, "API request rate limit exceeded")
        if origin and cors_origin is None:
            response = _error(request, 403, "Browser origin not permitted")
        elif request.method == "OPTIONS":
            if not cors_origin:
                response = _error(request, 403, "CORS origin not configured")
            else:
                response = web.Response(status=204)
                response.headers["Access-Control-Allow-Methods"] = (
                    "GET, POST, PUT, DELETE, OPTIONS"
                )
                response.headers["Access-Control-Allow-Headers"] = (
                    "Authorization, X-API-Key, Content-Type"
                )
                response.headers["Access-Control-Max-Age"] = "600"
        else:
            public = request.path in {"/api/versions", "/api/v1/health"}
            if not public:
                identity = _role(request, settings)
                if identity is None:
                    response = _error(request, 401, "Valid API credentials required")
                    response.headers["WWW-Authenticate"] = (
                        "Basic realm=anime-db-bot"
                        if settings.auth_mode == "basic"
                        else "Bearer realm=anime-db-bot"
                    )
                else:
                    role = identity
                    request["api_role"] = identity
                    response = await _bounded_handler(request, handler)
            else:
                request["api_role"] = "public"
                response = await _bounded_handler(request, handler)
    except ApiOperationError as exc:
        response = _error(request, exc.code, exc.message)
    except web.HTTPRequestEntityTooLarge:
        response = _error(request, 413, "Request body is too large")
    except asyncio.TimeoutError:
        response = _error(request, 504, "API request timed out")
    except Exception:
        _LOG.exception(
            "Unhandled API request failure",
            extra={"event": "api_request", "outcome": "error"},
        )
        response = _error(request, 500, "Internal API error")

    if cors_origin:
        response.headers["Access-Control-Allow-Origin"] = cors_origin
        response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Request-ID"] = request["request_id"]
    # Route templates or fixed labels only; never credentials, query strings,
    # raw IDs, headers or attacker-provided path values in the audit log.
    resource = request.match_info.route.resource
    route_label = getattr(resource, "canonical", "") or "unknown"
    _LOG.info(
        "HTTP request complete",
        extra={
            "event": "api_request",
            "platform": "api",
            "action": route_label,
            "operation": request.method,
            "outcome": str(response.status),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "state": role,
        },
    )
    return response

async def _bounded_handler(request: web.Request, handler: Any) -> web.Response:
    settings: ApiSettings = request.app["api_settings"]
    slots: asyncio.Semaphore = request.app["api_slots"]
    try:
        await asyncio.wait_for(slots.acquire(), timeout=0.1)
    except asyncio.TimeoutError as exc:
        raise ApiOperationError(503, "API concurrency limit reached") from exc
    try:
        return await asyncio.wait_for(handler(request), timeout=settings.timeout_seconds)
    finally:
        slots.release()


def _query(request: web.Request) -> dict[str, Any]:
    return dict(request.query)


async def _operation_response(
    request: web.Request, action: str, data: Mapping[str, Any] | None = None,
) -> web.Response:
    if action in LONG_OPERATIONS:
        result = request.app["api_jobs"].submit(
            request.app["context"], action, data or {}, request["api_role"]
        )
        response = _envelope(request, result)
        response.set_status(202)
        return response
    result = await execute_operation(
        request.app["context"], action, data,
        role=request["api_role"],
    )
    return _envelope(request, result)


async def _health(request: web.Request) -> web.Response:
    return _envelope(request, {"status": "ok", "version": "v1"})


async def _versions(request: web.Request) -> web.Response:
    return _envelope(request, {"versions": ["v1"], "preferred": "v1"})


async def _root(request: web.Request) -> web.Response:
    return _envelope(request, {
        "version": "v1",
        "openapi": "/api/v1/openapi.json",
        "graphql": "/graphql",
        "versioning": "Major changes will use /api/v2 instead of breaking v1.",
    })


async def _openapi(request: web.Request) -> web.Response:
    # OpenAPI clients require the document at the JSON root (no REST envelope).
    return web.json_response(openapi_document(request.app["api_settings"].public_url))


async def _operations(request: web.Request) -> web.Response:
    role = request["api_role"]
    return _envelope(request, {
        name: {"scope": spec.scope, "description": spec.description,
               "writes": spec.writes}
        for name, spec in OPERATIONS.items()
        if role == "admin" or spec.scope == "read"
    })


async def _generic(request: web.Request) -> web.Response:
    return await _operation_response(
        request, request.match_info["operation"], await _body(request)
    )


async def _get(request: web.Request) -> web.Response:
    return await _operation_response(
        request, request["api_action"], _query(request)
    )


async def _post(request: web.Request) -> web.Response:
    return await _operation_response(
        request, request["api_action"], await _body(request)
    )


def _wrap(action: str, *, body: bool = False) -> Any:
    async def handler(request: web.Request) -> web.Response:
        request["api_action"] = action
        return await _post(request) if body else await _get(request)
    return handler


async def _anime_by_id(request: web.Request) -> web.Response:
    source = request.match_info["source"]
    anime_id = request.match_info["id"]
    if not source.isascii() or not source.isalnum() or len(source) > 40:
        raise ApiOperationError(400, "Invalid source identifier")
    if not anime_id.isascii() or len(anime_id) > 80 or not anime_id.isdigit():
        raise ApiOperationError(400, "Source ID must be numeric")
    data = _query(request)
    data["query"] = f"{source}:{anime_id}"
    if request.match_info.get("section"):
        data["section"] = request.match_info["section"]
    return await _operation_response(request, "anime", data)


async def _user_operation(
    request: web.Request, action: str, *, body: bool = False,
) -> web.Response:
    data: dict[str, Any] = await _body(request) if body else _query(request)
    data["user_id"] = request.match_info["user_id"]
    return await _operation_response(request, action, data)


async def _config_operation(
    request: web.Request, action: str, *, body: bool = False,
) -> web.Response:
    data: dict[str, Any] = await _body(request) if body else _query(request)
    data["key"] = request.match_info["key"]
    return await _operation_response(request, action, data)


async def _index_operation(request: web.Request) -> web.Response:
    kind = request.match_info["kind"]
    if kind not in {"imdb", "baha"}:
        raise ApiOperationError(404, "Unknown index kind")
    return await _operation_response(
        request, "index." + kind, await _body(request)
    )


async def _jobs(request: web.Request) -> web.Response:
    return _envelope(
        request, request.app["api_jobs"].list_jobs(request["api_role"])
    )


async def _job(request: web.Request) -> web.Response:
    return _envelope(
        request,
        request.app["api_jobs"].job(
            request.match_info["job_id"], request["api_role"]
        ),
    )


async def _graphql(request: web.Request) -> web.Response:
    data = await _body(request)
    query = data.get("query")
    variables = data.get("variables")
    operation_name = data.get("operationName")
    if not isinstance(query, str):
        raise ApiOperationError(400, "GraphQL query must be text")
    if variables is not None and not isinstance(variables, dict):
        raise ApiOperationError(400, "GraphQL variables must be a JSON object")
    if operation_name is not None and not isinstance(operation_name, str):
        raise ApiOperationError(400, "operationName must be text")
    result = await execute_graphql(
        request.app["context"],
        role=request["api_role"],
        query=query,
        variables=variables,
        operation_name=operation_name,
        jobs=request.app["api_jobs"],
    )
    # GraphQL responses follow the GraphQL over HTTP payload, without REST envelope.
    return web.json_response(result)


def create_app(
    settings: AppSettings | None = None,
    *, context: AppContext | None = None,
) -> web.Application:
    """Build a testable HTTP application with optional shared service context."""
    cfg = settings or (context.settings if context is not None else get_settings())
    cfg.api.validate_server()

    app = web.Application(
        middlewares=[guard],
        client_max_size=cfg.api.max_body_bytes,
    )
    app["api_settings"] = cfg.api
    app["api_limiter"] = _WindowLimiter(cfg.api.requests_per_minute)
    app["api_slots"] = asyncio.Semaphore(cfg.api.max_concurrency)
    app["api_jobs"] = JobManager()
    app["context"] = context
    app["context_owned"] = context is None

    async def startup(server: web.Application) -> None:
        if server["context"] is None:
            server["context"] = AppContext.create(cfg)

    async def cleanup(server: web.Application) -> None:
        await server["api_jobs"].close()
        if server["context_owned"] and server["context"] is not None:
            await server["context"].aclose()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    router = app.router
    router.add_get("/api/versions", _versions)
    router.add_get("/api/v1/health", _health)
    router.add_get("/api/v1/", _root)
    router.add_get("/api/v1/openapi.json", _openapi)
    router.add_get("/api/v1/operations", _operations)
    router.add_post("/api/v1/actions/{operation}", _generic)
    router.add_get("/api/v1/jobs", _jobs)
    router.add_get("/api/v1/jobs/{job_id}", _job)
    router.add_get("/api/v1/search", _wrap("search"))
    router.add_get("/api/v1/lookup", _wrap("lookup"))
    router.add_get("/api/v1/anime", _wrap("anime"))
    router.add_get("/api/v1/anime/{source}/{id}", _anime_by_id)
    router.add_get("/api/v1/anime/{source}/{id}/{section}", _anime_by_id)
    router.add_post("/api/v1/images/recognize", _wrap("image", body=True))
    for route, action in (
        ("ping", "ping"), ("status", "status"),
        ("sources", "sources"), ("source-health", "source-health"),
        ("metrics", "metrics"), ("image-providers", "image-providers"),
    ):
        router.add_get("/api/v1/" + route, _wrap(action))
    router.add_get("/api/v1/users", _wrap("users"))
    router.add_get(
        "/api/v1/users/{user_id}",
        lambda r: _user_operation(r, "user"),
    )
    router.add_get(
        "/api/v1/users/{user_id}/language",
        lambda r: _user_operation(r, "language"),
    )
    router.add_put(
        "/api/v1/users/{user_id}/language",
        lambda r: _user_operation(r, "language.set", body=True),
    )
    router.add_post(
        "/api/v1/users/{user_id}/ban",
        lambda r: _user_operation(r, "ban", body=True),
    )
    router.add_delete(
        "/api/v1/users/{user_id}/ban",
        lambda r: _user_operation(r, "unban", body=True),
    )
    router.add_get("/api/v1/mappings/status", _wrap("mapping.status"))
    router.add_get("/api/v1/mappings/overrides", _wrap("mapping.list"))
    router.add_post(
        "/api/v1/mappings/overrides", _wrap("mapping.set", body=True)
    )
    router.add_delete(
        "/api/v1/mappings/overrides", _wrap("mapping.delete", body=True)
    )
    router.add_get("/api/v1/mappings/history", _wrap("mapping.history"))
    router.add_post(
        "/api/v1/mappings/update", _wrap("mapping.update", body=True)
    )
    router.add_get("/api/v1/config", _wrap("config.list"))
    router.add_get("/api/v1/config/check", _wrap("config.check"))
    router.add_get(
        "/api/v1/config/{key}",
        lambda r: _config_operation(r, "config.get"),
    )
    router.add_put(
        "/api/v1/config/{key}",
        lambda r: _config_operation(r, "config.set", body=True),
    )
    router.add_delete(
        "/api/v1/config/{key}",
        lambda r: _config_operation(r, "config.reset", body=True),
    )
    router.add_get("/api/v1/logs", _wrap("logs"))
    router.add_post("/api/v1/indexes/{kind}", _index_operation)
    router.add_post("/graphql", _graphql)
    return app


async def start_embedded_api(context: AppContext) -> web.AppRunner:
    """Start an API listener within the Telegram worker; caller owns cleanup."""
    app = create_app(context=context)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(
            runner, context.settings.api.host, context.settings.api.port,
        )
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    _LOG.info(
        "REST v1 and GraphQL API listening on %s:%d",
        context.settings.api.host, context.settings.api.port,
    )
    return runner


def run_api(settings: AppSettings | None = None) -> None:
    """Dedicated foreground API mode: no Telegram token or polling required."""
    cfg = settings or get_settings()
    cfg.api.validate_server()
    print(
        "[anime-db-bot api] REST v1: "
        f"http://{cfg.api.host}:{cfg.api.port}/api/v1/ | "
        f"GraphQL: http://{cfg.api.host}:{cfg.api.port}/graphql",
        flush=True,
    )
    web.run_app(
        create_app(cfg),
        host=cfg.api.host,
        port=cfg.api.port,
        print=lambda message: print(
            "[anime-db-bot api] " + message, flush=True
        ),
        access_log=None,
    )
