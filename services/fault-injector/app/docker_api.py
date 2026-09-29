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

    async def _post(self, path: str, what: str, **kwargs) -> httpx.Response:
        response = await self._client.post(path, **kwargs)
        if response.status_code not in (200, 201, 204, 304):
            raise DockerError(f"{what}: HTTP {response.status_code} {response.text}")
        return response

    async def is_available(self, service: str) -> bool:
        """Running and not paused, i.e. it can answer requests."""
        state = (await self.container(service))["State"]
        return state["Running"] and not state["Paused"]

    async def stop(self, service: str) -> None:
        container = await self.container(service)
        if container["State"]["Running"]:
            await self._post(f"/containers/{container['Id']}/stop", f"stop {service}", params={"t": "2"})

    async def pause(self, service: str) -> None:
        container = await self.container(service)
        if not container["State"]["Running"]:
            raise DockerError(f"cannot pause {service}: not running")
        if not container["State"]["Paused"]:
            await self._post(f"/containers/{container['Id']}/pause", f"pause {service}")

    async def resume_and_wait_healthy(self, service: str, timeout: float) -> None:
        """Unpauses or starts the container, then waits for its healthcheck."""
        container = await self.container(service)
        if container["State"]["Paused"]:
            await self._post(f"/containers/{container['Id']}/unpause", f"unpause {service}")
        elif not container["State"]["Running"]:
            await self._post(f"/containers/{container['Id']}/start", f"start {service}")
        deadline = time.monotonic() + timeout
        while True:
            state = (await self.container(service))["State"]
            health = state.get("Health", {}).get("Status")
            if state["Running"] and not state["Paused"] and health in (None, "healthy"):
                return
            if time.monotonic() > deadline:
                raise DockerError(f"{service} not healthy after {timeout}s (health={health})")
            await asyncio.sleep(1)

    async def exec(self, service: str, cmd: list[str], wait: bool = True, timeout: float = 15) -> int | None:
        """Runs `cmd` inside the container; returns its exit code (None when not waiting)."""
        container = await self.container(service)
        created = await self._post(f"/containers/{container['Id']}/exec", f"exec in {service}",
                                   json={"Cmd": cmd, "AttachStdout": False, "AttachStderr": False})
        exec_id = created.json()["Id"]
        await self._post(f"/exec/{exec_id}/start", f"exec start in {service}", json={"Detach": True})
        if not wait:
            return None
        deadline = time.monotonic() + timeout
        while True:
            info = (await self._client.get(f"/exec/{exec_id}/json")).json()
            if not info["Running"]:
                return info["ExitCode"]
            if time.monotonic() > deadline:
                raise DockerError(f"exec in {service} did not finish in {timeout}s")
            await asyncio.sleep(0.2)
