import asyncio
import json
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app.catalog import FAULT_TYPES, check_target, conflict_key
from app.config import settings
from app.datastores import Postgres, Redis
from app.docker_api import Docker
from app.reconcile import Reconciler
from app.store import FaultStore, now


class _JsonFormatter(logging.Formatter):
    """Plain JSON to stdout. These logs are control plane: Alloy drops them, so they never reach Loki."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": now(), "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in record.__dict__.items() if k in ("fault_id", "type", "target", "error")})
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("fault-injector")

store = FaultStore(settings.database_path)
lock = asyncio.Lock()


class FaultRequest(BaseModel):
    type: str
    target: str | None = None
    parameters: dict = Field(default_factory=dict)
    experiment_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")


async def reconcile_loop(reconciler: Reconciler) -> None:
    while True:
        await asyncio.sleep(settings.reconcile_interval_seconds)
        try:
            async with lock:
                await reconciler.enforce(store.list("active"))
        except Exception as exc:
            logger.warning("reconcile_failed", extra={"error": repr(exc)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    docker = Docker(settings.docker_socket, settings.compose_project)
    postgres = Postgres(settings.admin_database_url, settings.reporting_database_url)
    app.state.reconciler = Reconciler(docker, store, postgres, Redis(settings.redis_url), settings.healthy_timeout_seconds,
                                      settings.edge_dir)
    task = asyncio.create_task(reconcile_loop(app.state.reconciler))
    yield
    task.cancel()
    await app.state.reconciler.aclose()
    await postgres.aclose()
    await docker.aclose()


app = FastAPI(title="fault-injector", lifespan=lifespan)


def validate(request: FaultRequest) -> tuple[str, dict]:
    fault_type = FAULT_TYPES.get(request.type)
    if fault_type is None:
        raise HTTPException(422, f"unknown fault type {request.type!r}; known: {sorted(FAULT_TYPES)}")
    target = request.target or fault_type.default_target
    if target not in fault_type.targets:
        raise HTTPException(422, f"target for {request.type} must be one of {sorted(fault_type.targets)}")
    try:
        params = fault_type.params(**request.parameters).model_dump()
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json(include_url=False)))
    error = check_target(request.type, target, params)
    if error:
        raise HTTPException(422, error)
    return target, params


@app.post("/faults/validate")
async def validate_fault(request: FaultRequest) -> dict:
    """Dry run: validates and fills defaults without applying anything (used by experiment-runner)."""
    target, params = validate(request)
    return {"type": request.type, "target": target, "parameters": params}


@app.post("/faults", status_code=201)
async def create_fault(request: FaultRequest):
    target, params = validate(request)
    async with lock:
        active = store.list("active")
        key = conflict_key(request.type, target)
        clash = next((f for f in active if conflict_key(f["type"], f["target"]) == key), None)
        if clash:
            raise HTTPException(409, f"conflicts with active fault {clash['id']} ({clash['type']} on {target})")
        timestamp = now()
        fault = store.insert({
            "id": f"flt-{secrets.token_hex(6)}",
            "experiment_id": request.experiment_id or f"adhoc-{secrets.token_hex(4)}",
            "type": request.type, "target": target, "parameters": params,
            "state": "active", "error": None, "created_at": timestamp, "updated_at": timestamp,
        })
        try:
            await app.state.reconciler.enforce([*active, fault])
        except Exception as exc:
            failed = store.update(fault["id"], "failed", repr(exc))
            try:  # undo whatever was partially applied
                await app.state.reconciler.revert(fault, active)
            except Exception as rollback_exc:  # the periodic loop keeps enforcing the remaining faults
                logger.warning("rollback_failed", extra={"error": repr(rollback_exc)})
            logger.error("fault_failed", extra={"fault_id": fault["id"], "error": repr(exc)})
            return JSONResponse(status_code=502, content={"detail": "could not apply fault", "fault": failed})
    logger.info("fault_injected", extra={"fault_id": fault["id"], "type": fault["type"], "target": target})
    return fault


@app.get("/faults")
async def list_faults(state: Literal["active", "removed", "failed"] | None = None) -> list[dict]:
    return store.list(state)


@app.get("/faults/{fault_id}")
async def get_fault(fault_id: str) -> dict:
    fault = store.get(fault_id)
    if fault is None:
        raise HTTPException(404, "fault not found")
    return fault


async def _remove(fault: dict) -> dict:
    """Reverts one active fault; it stays active (with an error) if the revert fails."""
    still_active = [f for f in store.list("active") if f["id"] != fault["id"]]
    try:
        await app.state.reconciler.revert(fault, still_active)
    except Exception as exc:
        store.update(fault["id"], "active", f"revert failed: {exc!r}")
        raise HTTPException(502, f"could not revert {fault['id']}: {exc!r}")
    logger.info("fault_removed", extra={"fault_id": fault["id"], "type": fault["type"], "target": fault["target"]})
    return store.update(fault["id"], "removed")


@app.delete("/faults/{fault_id}")
async def delete_fault(fault_id: str) -> dict:
    async with lock:
        fault = store.get(fault_id)
        if fault is None:
            raise HTTPException(404, "fault not found")
        if fault["state"] != "active":
            return fault
        return await _remove(fault)


@app.delete("/faults")
async def delete_all_faults() -> list[dict]:
    async with lock:
        return [await _remove(fault) for fault in store.list("active")]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fault-injector"}
