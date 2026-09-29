"""Drives the system towards the desired state implied by the active faults.

Runs after every API change and periodically, so faults survive restarts of the injector or its targets.
"""

import logging

import httpx

from app.catalog import FAULT_TYPES
from app.docker_api import Docker

logger = logging.getLogger("reconcile")


def payment_config(active: list[dict]) -> dict:
    """Merges active payment_* faults into the config accepted by payment's `PUT /__faults`."""
    config: dict = {}
    for fault in active:
        params = fault["parameters"]
        if fault["type"] == "payment_latency":
            config.update(
                latency_ms=params["latency_ms"],
                jitter_ms=params.get("jitter_ms", 0),
                latency_probability=params.get("probability", 1.0),
            )
        elif fault["type"] == "payment_error":
            config.update(error_status=params.get("status_code", 500), error_probability=params.get("probability", 1.0))
    return config


def stopped_targets(active: list[dict]) -> set[str]:
    return {f["target"] for f in active if FAULT_TYPES[f["type"]].mechanism == "container_stop"}


class Reconciler:
    def __init__(self, docker: Docker, payment_url: str):
        self.docker = docker
        self._payment = httpx.AsyncClient(base_url=payment_url, timeout=3)

    async def aclose(self) -> None:
        await self._payment.aclose()

    async def push_payment(self, active: list[dict]) -> None:
        config = payment_config(active)
        if "payment" in stopped_targets(active):
            return  # nothing to push to a stopped container; it gets the config once it is back
        try:
            if config:
                response = await self._payment.put("/__faults", json=config)
            else:
                response = await self._payment.delete("/__faults")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Payment may be restarting; the next periodic pass retries.
            logger.warning("payment_push_failed", extra={"error": repr(exc)})
            if config:
                raise

    async def enforce(self, active: list[dict]) -> None:
        """Applies faults. Raises when an active fault cannot be realized."""
        for target in stopped_targets(active):
            await self.docker.stop(target)
        await self.push_payment(active)

    async def revert(self, fault: dict, still_active: list[dict], healthy_timeout: float) -> None:
        """Undoes a single fault, given the faults that remain active."""
        mechanism = FAULT_TYPES[fault["type"]].mechanism
        if mechanism == "container_stop" and fault["target"] not in stopped_targets(still_active):
            await self.docker.start_and_wait_healthy(fault["target"], healthy_timeout)
        await self.push_payment(still_active)
