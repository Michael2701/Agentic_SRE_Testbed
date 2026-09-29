import logging
import secrets
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Header, HTTPException
from observability import instrument, track
from prometheus_client import Counter
from pydantic import BaseModel

from app.config import settings

TOKEN_PREFIX = "token:"

logger = logging.getLogger("auth")
LOGINS = Counter("logins_total", "Login attempts", ["result"])
VALIDATIONS = Counter("token_validations_total", "Token validations", ["result"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ValidateResponse(BaseModel):
    user_id: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="auth", lifespan=lifespan)
instrument(app, "auth")


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="invalid Authorization header")
    return token


@app.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    expected = settings.users.get(body.username)
    if expected is None or not secrets.compare_digest(expected, body.password):
        LOGINS.labels("failure").inc()
        logger.info("login_failed", extra={"username": body.username})
        raise HTTPException(status_code=401, detail="invalid credentials")

    token = secrets.token_urlsafe(32)
    async with track("redis", "set_token"):
        await app.state.redis.set(TOKEN_PREFIX + token, body.username, ex=settings.token_ttl_seconds)
    LOGINS.labels("success").inc()
    logger.info("login_succeeded", extra={"user_id": body.username})
    return LoginResponse(access_token=token, expires_in=settings.token_ttl_seconds)


@app.post("/validate", response_model=ValidateResponse)
async def validate(authorization: str | None = Header(default=None)) -> ValidateResponse:
    token = extract_bearer(authorization)
    async with track("redis", "get_token"):
        user_id = await app.state.redis.get(TOKEN_PREFIX + token)
    if user_id is None:
        VALIDATIONS.labels("invalid").inc()
        logger.info("token_invalid")
        raise HTTPException(status_code=401, detail="invalid or expired token")
    VALIDATIONS.labels("valid").inc()
    return ValidateResponse(user_id=user_id)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "auth"}


@app.get("/ready")
async def ready() -> dict:
    try:
        await app.state.redis.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"redis unavailable: {exc}")
    return {"status": "ready", "service": "auth", "checks": {"redis": "ok"}}
