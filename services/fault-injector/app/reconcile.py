"""Drives the system towards the desired state implied by the active faults.

`enforce` is idempotent and runs after every API change and every few seconds, so faults survive restarts
of the injector or of their targets. `revert` undoes a single fault given the faults that remain active.
"""

import asyncio
import logging

import httpx

from app.catalog import BACKGROUND_MECHANISMS, FAULT_TYPES, FAULTPOINT_SERVICES
from app.datastores import Postgres, Redis
from app.docker_api import Docker
from app.workers import Workers

logger = logging.getLogger("reconcile")


def mechanism(fault: dict) -> str:
    return FAULT_TYPES[fault["type"]].mechanism


def faultpoint_config(active: list[dict], service: str) -> dict:
    """Merges the active faultpoint faults of one service into its `PUT /__faults` body."""
    config: dict = {}
    for fault in active:
        if mechanism(fault) != "faultpoint" or fault["target"] != service:
            continue
        params = fault["parameters"]
        if fault["type"] == "payment_latency":
            config.update(latency_ms=params["latency_ms"], jitter_ms=params["jitter_ms"],
                          latency_probability=params["probability"])
        else:  # payment_error | intermittent_errors: the highest error probability wins
            probability = params.get("probability", params.get("error_rate"))
            if probability >= config.get("error_probability", 0):
                config.update(error_status=params["status_code"], error_probability=probability)
    return config


def container_targets(active: list[dict], kind: str) -> set[str]:
    return {f["target"] for f in active if mechanism(f) == kind}


class Reconciler:
    def __init__(self, docker: Docker, postgres: Postgres, redis_: Redis, healthy_timeout: float):
        self.docker = docker
        self.postgres = postgres
        self.redis = redis_
        self.workers = Workers(docker)
        self.healthy_timeout = healthy_timeout
        self._http = httpx.AsyncClient(timeout=3)
        self._tasks: dict[str, asyncio.Task] = {}

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self._http.aclose()

    # ---------------------------------------------------------------- enforce

    async def enforce(self, active: list[dict]) -> None:
        """Applies all active faults. Raises when an active fault cannot be realized."""
        for target in container_targets(active, "container_stop"):
            await self.docker.stop(target)
        for target in container_targets(active, "container_pause"):
            await self.docker.pause(target)
        for fault in active:
            kind = mechanism(fault)
            if kind in ("exec_cpu", "exec_memory"):
                await self.workers.ensure(fault)
            elif kind == "pg_trigger":
                await self.postgres.ensure_slow_trigger(fault["parameters"]["delay_ms"], fault["parameters"]["operations"])
            elif kind in BACKGROUND_MECHANISMS:
                self._ensure_task(fault)
        await self.push_faultpoints(active)

    async def push_faultpoints(self, active: list[dict]) -> None:
        unreachable = container_targets(active, "container_stop") | container_targets(active, "container_pause")
        for service in FAULTPOINT_SERVICES - unreachable:
            config = faultpoint_config(active, service)
            try:
                if config:
                    response = await self._http.put(f"http://{service}:8000/__faults", json=config)
                else:
                    response = await self._http.delete(f"http://{service}:8000/__faults")
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # The service may be restarting; the next periodic pass retries.
                logger.warning("faultpoint_push_failed", extra={"target": service, "error": repr(exc)})
                if config:
                    raise

    def _ensure_task(self, fault: dict) -> None:
        task = self._tasks.get(fault["id"])
        if task is not None and not task.done():
            return
        if task is not None and not task.cancelled() and task.exception() is not None:
            logger.warning("background_restarted", extra={"fault_id": fault["id"], "error": repr(task.exception())})
        params = fault["parameters"]
        kind = mechanism(fault)
        if kind == "pg_exhaustion":
            coro = self.postgres.hold_connections()
        elif kind == "pg_lock":
            coro = self.postgres.lock_loop(params["hold_ms"], params["interval_ms"])
        else:
            coro = self.redis.pause_loop(params["pause_ms"], params["interval_ms"])
        self._tasks[fault["id"]] = asyncio.create_task(coro)

    # ---------------------------------------------------------------- revert

    async def revert(self, fault: dict, still_active: list[dict]) -> None:
        kind = mechanism(fault)
        target = fault["target"]
        if kind == "container_stop" and target not in container_targets(still_active, "container_stop"):
            await self.docker.resume_and_wait_healthy(target, self.healthy_timeout)
        elif kind == "container_pause" and target not in container_targets(still_active, "container_pause"):
            await self.docker.resume_and_wait_healthy(target, self.healthy_timeout)
        elif kind in ("exec_cpu", "exec_memory"):
            await self.workers.kill(target, fault["id"])
        elif kind == "pg_trigger":
            await self.postgres.drop_slow_trigger()
        elif kind in BACKGROUND_MECHANISMS:
            task = self._tasks.pop(fault["id"], None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.push_faultpoints(still_active)
