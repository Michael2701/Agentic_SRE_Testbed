"""Drives the system towards the desired state implied by the active faults.

`enforce` is idempotent and runs after every API change and every few seconds, so faults survive restarts
of the injector or of their targets; it reports errors per fault instead of stopping at the first one.
`revert` undoes a single fault given the faults that remain active.
"""

import asyncio
import logging

import httpx

from app.catalog import BACKGROUND_MECHANISMS, FAULT_TYPES, FAULTPOINT_SERVICES
from app.datastores import Postgres, Redis
from app.deploy import Deployer
from app.docker_api import Docker, DockerError
from app.edge import Edge
from app.limits import CpuLimits
from app.network import Network
from app.store import FaultStore
from app.workers import Workers

logger = logging.getLogger("reconcile")


def mechanism(fault: dict) -> str:
    return FAULT_TYPES[fault["type"]].mechanism


def faultpoint_config(active: list[dict], service: str) -> dict:
    """Merges the active faultpoint faults of one service into its `PUT /__faults` body."""
    config: dict = {}

    def add_error(status: int, probability: float) -> None:  # the highest error probability wins
        if probability >= config.get("error_probability", 0):
            config.update(error_status=status, error_probability=probability)

    for fault in active:
        if fault["target"] != service:
            continue
        params = fault["parameters"]
        if fault["type"] == "payment_latency":
            config.update(latency_ms=params["latency_ms"], jitter_ms=params["jitter_ms"],
                          latency_probability=params["probability"])
        elif fault["type"] in ("payment_error", "intermittent_errors"):
            add_error(params["status_code"], params.get("probability", params.get("error_rate")))
        elif fault["type"] == "bad_deployment" and params["defect"] == "errors":  # the regression of the release
            add_error(500, params["error_rate"])
        elif fault["type"] == "bad_deployment" and params["defect"] == "slow":
            config.update(latency_ms=params["latency_ms"], jitter_ms=params["latency_ms"] // 4, latency_probability=1.0)
    return config


def faultpoint_faults(active: list[dict], service: str) -> list[dict]:
    """The active faults that contribute to `faultpoint_config(active, service)`."""
    return [f for f in active if f["target"] == service and (
        mechanism(f) == "faultpoint" or (f["type"] == "bad_deployment" and f["parameters"]["defect"] in ("errors", "slow")))]


def container_targets(active: list[dict], kind: str) -> set[str]:
    return {f["target"] for f in active if mechanism(f) == kind}


class Reconciler:
    def __init__(self, docker: Docker, store: FaultStore, postgres: Postgres, redis_: Redis, healthy_timeout: float,
                 edge_dir: str):
        self.docker = docker
        self.postgres = postgres
        self.redis = redis_
        self.workers = Workers(docker)
        self.network = Network(docker)
        self.deployer = Deployer(docker, store)
        self.edge = Edge(docker, edge_dir)
        self.cpu_limits = CpuLimits(docker, store)
        self.healthy_timeout = healthy_timeout
        self._http = httpx.AsyncClient(timeout=3)
        self._tasks: dict[str, asyncio.Task] = {}

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self._http.aclose()

    # ---------------------------------------------------------------- enforce

    async def enforce(self, active: list[dict]) -> dict[str, str]:
        """Applies all active faults; returns {fault id: error} for those that could not be realized.

        Each step is isolated: one fault that cannot be realized (e.g. its container is gone) never keeps the
        others from being enforced, and it never fails a new fault that has nothing to do with it.
        """
        errors: dict[str, str] = {}

        async def attempt(faults: list[dict], step) -> None:
            try:
                await step
            except Exception as exc:
                if not faults:
                    logger.warning("enforce_step_failed", extra={"error": repr(exc)})
                for fault in faults:
                    errors.setdefault(fault["id"], repr(exc))

        for fault in active:
            if mechanism(fault) == "redeploy":
                await attempt([fault], self._ensure_release(fault, active))
        for kind, action in (("container_stop", self.docker.stop), ("container_pause", self.docker.pause)):
            for target in container_targets(active, kind):
                await attempt([f for f in active if mechanism(f) == kind and f["target"] == target], action(target))
        for fault in active:
            kind = mechanism(fault)
            if kind in ("exec_cpu", "exec_memory"):
                await attempt([fault], self.workers.ensure(fault))
            elif kind == "pg_trigger":
                params = fault["parameters"]
                await attempt([fault], self.postgres.ensure_slow_trigger(params["delay_ms"], params["operations"]))
            elif kind == "netns":
                await attempt([fault], self.network.ensure(fault))
            elif kind == "cpu_quota":
                await attempt([fault], self.cpu_limits.ensure(fault))
            elif kind in BACKGROUND_MECHANISMS:
                self._ensure_task(fault)
        await attempt([f for f in active if mechanism(f) == "edge_config"], self.edge.ensure(active))
        for service, error in (await self.push_faultpoints(active)).items():
            for fault in faultpoint_faults(active, service):
                errors.setdefault(fault["id"], error)
        return errors

    async def _ensure_release(self, fault: dict, active: list[dict]) -> None:
        if not await self.deployer.ensure(fault):
            return
        target = fault["target"]
        if fault["type"] == "bad_deployment" and fault["parameters"]["defect"] != "crash":
            # the release's behaviour is pushed to its faultpoint below, so it must be up first
            await self.docker.wait_healthy(target, self.healthy_timeout)
        elif faultpoint_config(active, target):
            # other faults' faultpoint config is pushed below; a release that never gets healthy is this fault's
            # symptom, not an error, so the push failure is left to the faultpoint faults (and retried)
            try:
                await self.docker.wait_healthy(target, self.healthy_timeout)
            except DockerError as exc:
                logger.warning("redeploy_not_healthy", extra={"target": target, "error": repr(exc)})

    async def push_faultpoints(self, active: list[dict]) -> dict[str, str]:
        """Pushes every faultpoint service its config; returns {service: error} for failed non-empty pushes.

        A failed reset (empty config) is only logged: the next periodic pass pushes again anyway.
        """
        unreachable = container_targets(active, "container_stop") | container_targets(active, "container_pause")
        unreachable |= {f["target"] for f in active if f["type"] == "bad_deployment" and f["parameters"]["defect"] == "crash"}
        errors: dict[str, str] = {}
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
                    errors[service] = repr(exc)
        return errors

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

    async def revert(self, fault: dict, still_active: list[dict]) -> str | None:
        """Undoes one fault. Raises when the undo fails; returns a warning when it worked but the target did not
        become healthy in time (the fault is gone all the same, so re-applying it would be wrong)."""
        kind = mechanism(fault)
        target = fault["target"]
        warning = None
        if kind in ("container_stop", "container_pause") and target not in container_targets(still_active, kind):
            await self.docker.resume(target)
            warning = await self._settle(target)
        elif kind == "redeploy":
            await self.deployer.restore(target)
            warning = await self._settle(target)
        elif kind == "netns":
            await self.network.clear(fault)
        elif kind == "cpu_quota":
            await self.cpu_limits.restore(target)
        elif kind == "edge_config":
            await self.edge.ensure(still_active)
        elif kind in ("exec_cpu", "exec_memory"):
            await self.workers.kill(target, fault["id"])
        elif kind == "pg_trigger":
            await self.postgres.drop_slow_trigger()
        elif kind in BACKGROUND_MECHANISMS:
            task = self._tasks.pop(fault["id"], None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.push_faultpoints(still_active)  # failures belong to the remaining faults; the loop retries
        return warning

    async def _settle(self, target: str) -> str | None:
        try:
            await self.docker.wait_healthy(target, self.healthy_timeout)
        except DockerError as exc:
            logger.warning("revert_not_healthy", extra={"target": target, "error": repr(exc)})
            return f"reverted, but {exc}"
        return None
