"""Control-plane fault hook: behaviour the Fault Injector pushes to a service (latency and/or 5xx).

Only symptoms may be observable. Nothing here logs, counts or annotates spans, and `/__*` paths are
excluded from telemetry by libs/observability. The desired state is re-pushed by the injector's
reconcile loop, so an in-memory config is enough (a restart clears it until the next push).

Call `install(app)` BEFORE `observability.instrument(app, ...)`: middleware added later wraps earlier
middleware, so the service's own access log / RED metrics then record the injected latency and 5xx.
"""

import asyncio
import json
import random

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field
from starlette.routing import Match

# Probes and scrapes are never faulted (a failing /health would look like a different fault type).
EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics"})


class FaultConfig(BaseModel):
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    jitter_ms: int = Field(default=0, ge=0, le=30_000)
    latency_probability: float = Field(default=1.0, ge=0, le=1)
    error_status: int | None = Field(default=None, ge=500, le=599)
    error_probability: float = Field(default=1.0, ge=0, le=1)


class _State:
    config = FaultConfig()


router = APIRouter(prefix="/__faults", include_in_schema=False)


@router.put("")
async def set_config(config: FaultConfig) -> FaultConfig:
    _State.config = config
    return config


@router.delete("")
async def clear_config() -> FaultConfig:
    _State.config = FaultConfig()
    return _State.config


@router.get("")
async def get_config() -> FaultConfig:
    return _State.config


class FaultpointMiddleware:
    """Pure ASGI: delays and/or fails business requests per the current config."""

    def __init__(self, app, routes):
        self.app = app
        self.routes = routes

    def _resolve_route(self, scope) -> None:
        # A short-circuited response never reaches the router, so `scope["route"]` would stay unset and
        # telemetry would label it route="unmatched", a tell-tale sign. Resolve it the way the router does.
        for route in self.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                return

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path in EXEMPT_PATHS or path.startswith("/__"):
            await self.app(scope, receive, send)
            return

        config = _State.config
        if config.latency_ms and random.random() < config.latency_probability:
            jitter = random.uniform(-config.jitter_ms, config.jitter_ms) if config.jitter_ms else 0
            await asyncio.sleep(max(0.0, config.latency_ms + jitter) / 1000)
        if config.error_status and random.random() < config.error_probability:
            self._resolve_route(scope)
            body = json.dumps({"detail": "internal server error"}).encode()
            await send({"type": "http.response.start", "status": config.error_status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.add_middleware(FaultpointMiddleware, routes=app.router.routes)
