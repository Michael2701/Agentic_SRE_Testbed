import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from observability import annotate_span, instrument
from observability.http import instrumented_client
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app.config import settings

logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.auth = instrumented_client(settings.auth_url, "auth", settings.http_timeout_seconds)
    app.state.order = instrumented_client(settings.order_url, "order", settings.http_timeout_seconds)
    # Readiness probes stay out of dependency metrics, logs and traces, like inbound probes (middleware.py).
    app.state.probe = httpx.AsyncClient(timeout=settings.ready_timeout_seconds)
    HTTPXClientInstrumentor.uninstrument_client(app.state.probe)
    yield
    for client in (app.state.auth, app.state.order, app.state.probe):
        await client.aclose()


class ForwardedPrefix:
    """Behind the portal (http://localhost:8000/<prefix>/) the prefix is stripped and sent as X-Forwarded-Prefix;
    as root_path it makes Swagger UI load its OpenAPI document (and "Try it out" call) under that prefix."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1")
            if prefix.startswith("/") and prefix.isascii():
                prefix = prefix.rstrip("/")  # ASGI: `path` includes `root_path` (routing strips it again)
                scope = {**scope, "root_path": prefix, "path": prefix + scope["path"],
                         "raw_path": prefix.encode() + scope.get("raw_path", scope["path"].encode())}
        await self.app(scope, receive, send)


app = FastAPI(title="gateway", lifespan=lifespan)
app.add_middleware(ForwardedPrefix)  # before instrument(): access logs and probe filtering see the plain path
instrument(app, "gateway")


async def call(client: httpx.AsyncClient, name: str, method: str, path: str, **kwargs) -> httpx.Response:
    """Performs a downstream call, mapping transport failures to 502/504."""
    try:
        return await client.request(method, path, **kwargs)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"{name} timed out")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{name} unavailable: {exc!r}")


def relay(response: httpx.Response) -> JSONResponse:
    try:
        content = response.json()
    except ValueError:
        content = {"detail": response.text}
    return JSONResponse(status_code=response.status_code, content=content)


async def read_json(request: Request):
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="request body must be valid JSON")


async def authenticate(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    response = await call(
        app.state.auth, "auth", "POST", "/validate", headers={"Authorization": authorization}
    )
    if response.status_code == 401:
        logger.info("auth_rejected")
        raise HTTPException(status_code=401, detail="invalid or expired token")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"auth returned {response.status_code}")
    try:
        user_id = response.json()["user_id"]
    except (ValueError, KeyError):
        raise HTTPException(status_code=502, detail="auth returned an invalid response")
    annotate_span({"user.id": user_id})
    return user_id


@app.post("/login")
async def login(request: Request) -> JSONResponse:
    response = await call(app.state.auth, "auth", "POST", "/login", json=await read_json(request))
    return relay(response)


@app.post("/orders")
async def create_order(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    user_id = await authenticate(authorization)
    response = await call(
        app.state.order, "order", "POST", "/orders",
        json=await read_json(request), headers={"X-User-Id": user_id},
    )
    return relay(response)


@app.get("/orders/{order_id}")
async def read_order(order_id: uuid.UUID, authorization: str | None = Header(default=None)) -> JSONResponse:
    user_id = await authenticate(authorization)
    response = await call(
        app.state.order, "order", "GET", f"/orders/{order_id}", headers={"X-User-Id": user_id}
    )
    return relay(response)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "gateway"}


@app.get("/ready")
async def ready() -> JSONResponse:
    async def check(url: str) -> str:
        try:
            response = await app.state.probe.get(f"{url}/ready")
            return "ok" if response.status_code == 200 else f"status {response.status_code}"
        except httpx.HTTPError as exc:
            return f"error: {exc!r}"

    results = await asyncio.gather(check(settings.auth_url), check(settings.order_url))
    checks = dict(zip(("auth", "order"), results))
    ok = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "service": "gateway", "checks": checks},
    )
