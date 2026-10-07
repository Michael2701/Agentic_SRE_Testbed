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


ENFORCE_ERROR = "enforce failed: "


def record_enforce_errors(active: list[dict], errors: dict[str, str]) -> None:
    """Shows on each active fault whether the last pass could realize it (the error clears once it can)."""
    for fault in active:
        current = fault["error"] or ""
        if fault["id"] in errors:
            wanted = ENFORCE_ERROR + errors[fault["id"]]
        elif current.startswith(ENFORCE_ERROR):
            wanted = None
        else:
            continue  # keeps e.g. a "revert failed" note
        if wanted != fault["error"]:
            store.update(fault["id"], "active", wanted)


async def reconcile_loop(reconciler: Reconciler) -> None:
    while True:
        await asyncio.sleep(settings.reconcile_interval_seconds)
        try:
            async with lock:
                active = store.list("active")
                record_enforce_errors(active, await reconciler.enforce(active))
        except Exception as exc:
            logger.warning("reconcile_failed", extra={"error": repr(exc)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    docker = Docker(settings.docker_socket, settings.compose_project)
    postgres = Postgres(settings.admin_database_url, settings.reporting_database_url)
    app.state.reconciler = Reconciler(docker, store, postgres, Redis(settings.redis_url), settings.healthy_timeout_seconds,
                                      settings.edge_dir)
    try:
        await docker.sweep()
    except Exception as exc:
        logger.warning("sweep_failed", extra={"error": repr(exc)})
    task = asyncio.create_task(reconcile_loop(app.state.reconciler))
    yield
    async with lock:  # never cancel the loop mid-operation (e.g. between the steps of a redeploy)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
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
        # a fault awaiting cleanup still holds what it partially applied, so it blocks like an active one
        clash = next((f for f in [*active, *store.pending_cleanup()] if conflict_key(f["type"], f["target"]) == key), None)
        if clash:
            raise HTTPException(409, f"conflicts with {clash['state']} fault {clash['id']} ({clash['type']} on {target})"
                                     + ("; DELETE it to finish its cleanup" if clash["needs_cleanup"] else ""))
        timestamp = now()
        fault = store.insert({
            "id": f"flt-{secrets.token_hex(6)}",
            "experiment_id": request.experiment_id or f"adhoc-{secrets.token_hex(4)}",
            "type": request.type, "target": target, "parameters": params,
            "state": "active", "error": None, "created_at": timestamp, "updated_at": timestamp,
        })
        errors = await app.state.reconciler.enforce([*active, fault])
        record_enforce_errors(active, errors)
        if fault["id"] in errors:
            error = errors[fault["id"]]
            try:  # undo whatever was partially applied
                await app.state.reconciler.revert(fault, active)
                failed = store.update(fault["id"], "failed", error)
            except Exception as rollback_exc:  # DELETE retries it (see _cleanup)
                logger.warning("rollback_failed", extra={"fault_id": fault["id"], "error": repr(rollback_exc)})
                failed = store.update(fault["id"], "failed", f"{error}; rollback failed: {rollback_exc!r}", True)
            logger.error("fault_failed", extra={"fault_id": fault["id"], "error": error})
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


class RevertError(Exception):
    def __init__(self, fault: dict):
        super().__init__(fault["error"])
        self.fault = fault


async def _remove(fault: dict) -> dict:
    """Reverts one active fault; it stays active (with an error) if the revert fails."""
    still_active = [f for f in store.list("active") if f["id"] != fault["id"]]
    try:
        warning = await app.state.reconciler.revert(fault, still_active)
    except Exception as exc:
        raise RevertError(store.update(fault["id"], "active", f"revert failed: {exc!r}"))
    logger.info("fault_removed", extra={"fault_id": fault["id"], "type": fault["type"], "target": fault["target"]})
    return store.update(fault["id"], "removed", warning)


async def _cleanup(fault: dict) -> dict:
    """Retries undoing what a failed fault partially applied; it stays failed either way."""
    error = fault["error"].split("; rollback failed: ")[0]
    try:
        await app.state.reconciler.revert(fault, store.list("active"))
    except Exception as exc:
        raise RevertError(store.update(fault["id"], "failed", f"{error}; rollback failed: {exc!r}", True))
    logger.info("fault_cleaned_up", extra={"fault_id": fault["id"], "type": fault["type"], "target": fault["target"]})
    return store.update(fault["id"], "failed", f"{error}; cleaned up")


async def _undo(fault: dict) -> dict:
    return await (_remove(fault) if fault["state"] == "active" else _cleanup(fault))


@app.delete("/faults/{fault_id}")
async def delete_fault(fault_id: str) -> dict:
    async with lock:
        fault = store.get(fault_id)
        if fault is None:
            raise HTTPException(404, "fault not found")
        if fault["state"] != "active" and not fault["needs_cleanup"]:
            return fault
        try:
            return await _undo(fault)
        except RevertError as exc:
            raise HTTPException(502, f"could not revert {fault_id}: {exc}")


@app.delete("/faults")
async def delete_all_faults():
    """Reverts every active fault (and finishes pending cleanups). Every fault is tried; one that fails is
    retried once after the others, since a revert can depend on another (e.g. workers in a paused container)."""
    async with lock:
        results: dict[str, dict] = {}
        pending = [*store.list("active"), *store.pending_cleanup()]
        for attempt in range(2):
            failed = []
            for fault in pending:
                try:
                    results[fault["id"]] = await _undo(fault)
                except RevertError as exc:
                    results[fault["id"]] = exc.fault
                    failed.append(exc.fault)
            pending = failed
            if not pending:
                return list(results.values())
        return JSONResponse(status_code=502, content={
            "detail": f"could not revert {', '.join(f['id'] for f in pending)}", "faults": list(results.values())})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fault-injector"}
