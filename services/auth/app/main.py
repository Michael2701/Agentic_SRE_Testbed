import secrets
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from app.config import settings

TOKEN_PREFIX = "token:"


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
        raise HTTPException(status_code=401, detail="invalid credentials")

    token = secrets.token_urlsafe(32)
    await app.state.redis.set(TOKEN_PREFIX + token, body.username, ex=settings.token_ttl_seconds)
    return LoginResponse(access_token=token, expires_in=settings.token_ttl_seconds)


@app.post("/validate", response_model=ValidateResponse)
async def validate(authorization: str | None = Header(default=None)) -> ValidateResponse:
    token = extract_bearer(authorization)
    user_id = await app.state.redis.get(TOKEN_PREFIX + token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
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
