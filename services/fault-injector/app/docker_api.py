"""Minimal Docker Engine API client over the unix socket (no docker SDK dependency)."""

import asyncio
import json
import time

import httpx


class DockerError(Exception):
    pass


class Docker:
    def __init__(self, socket_path: str, project: str):
        self._client = httpx.AsyncClient(
            base_url="http://docker", transport=httpx.AsyncHTTPTransport(uds=socket_path), timeout=30
        )
        self.project = project

    async def aclose(self) -> None:
        await self._client.aclose()

    async def container(self, service: str) -> dict:
        """Inspect data of the compose service's container."""
        filters = {"label": [f"com.docker.compose.project={self.project}", f"com.docker.compose.service={service}"]}
        response = await self._client.get("/containers/json", params={"all": "1", "filters": json.dumps(filters)})
        response.raise_for_status()
        containers = response.json()
        if not containers:
            raise DockerError(f"no container for service {service!r}")
        inspect = await self._client.get(f"/containers/{containers[0]['Id']}/json")
        inspect.raise_for_status()
        return inspect.json()

    async def is_running(self, service: str) -> bool:
        return (await self.container(service))["State"]["Running"]

    async def stop(self, service: str) -> None:
        container = await self.container(service)
        if container["State"]["Running"]:
            response = await self._client.post(f"/containers/{container['Id']}/stop", params={"t": "2"})
            if response.status_code not in (204, 304):
                raise DockerError(f"stop {service}: HTTP {response.status_code} {response.text}")

    async def start_and_wait_healthy(self, service: str, timeout: float) -> None:
        container = await self.container(service)
        if not container["State"]["Running"]:
            response = await self._client.post(f"/containers/{container['Id']}/start")
            if response.status_code not in (204, 304):
                raise DockerError(f"start {service}: HTTP {response.status_code} {response.text}")
        deadline = time.monotonic() + timeout
        while True:
            state = (await self.container(service))["State"]
            health = state.get("Health", {}).get("Status")
            if state["Running"] and health in (None, "healthy"):
                return
            if time.monotonic() > deadline:
                raise DockerError(f"{service} not healthy after {timeout}s (health={health})")
            await asyncio.sleep(1)
