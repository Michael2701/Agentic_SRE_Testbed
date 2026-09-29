"""Control-plane hook: behaviour the Fault Injector pushes to this service.

Only symptoms may be observable: nothing here logs, counts or annotates spans. `/__*` paths are
excluded from telemetry by libs/observability. The desired state is re-pushed by the injector's
reconcile loop, so an in-memory config is enough (a restart clears it until the next push).
"""

import asyncio
import random

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class FaultConfig(BaseModel):
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    jitter_ms: int = Field(default=0, ge=0, le=30_000)
    latency_probability: float = Field(default=1.0, ge=0, le=1)
    error_status: int | None = Field(default=None, ge=500, le=599)
    error_probability: float = Field(default=1.0, ge=0, le=1)


current = FaultConfig()
router = APIRouter(prefix="/__faults", include_in_schema=False)


@router.put("")
async def set_config(config: FaultConfig) -> FaultConfig:
    global current
    current = config
    return current


@router.delete("")
async def clear_config() -> FaultConfig:
    global current
    current = FaultConfig()
    return current


@router.get("")
async def get_config() -> FaultConfig:
    return current


async def apply() -> JSONResponse | None:
    """Delays and/or returns an error response per the current config; None means proceed normally."""
    config = current
    if config.latency_ms and random.random() < config.latency_probability:
        jitter = random.uniform(-config.jitter_ms, config.jitter_ms) if config.jitter_ms else 0
        await asyncio.sleep(max(0.0, config.latency_ms + jitter) / 1000)
    if config.error_status and random.random() < config.error_probability:
        return JSONResponse(status_code=config.error_status, content={"detail": "payment processor error"})
    return None
