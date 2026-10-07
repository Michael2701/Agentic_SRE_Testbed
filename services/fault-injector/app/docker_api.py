"""Minimal Docker Engine API client over the unix socket (no docker SDK dependency)."""

import asyncio
import json
import logging
import os
import time

import httpx

logger = logging.getLogger("docker")

HELPER_LABEL = "io.testbed.helper"
PREV_SUFFIX = "-prev"  # the replaced container during a redeploy, see `recreate`


class DockerError(Exception):
    pass


class Docker:
    def __init__(self, socket_path: str, project: str):
        self._client = httpx.AsyncClient(
            base_url="http://docker", transport=httpx.AsyncHTTPTransport(uds=socket_path), timeout=30
        )
        self.project = project
        self._self_image: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _list(self, labels: list[str]) -> list[dict]:
        response = await self._client.get("/containers/json", params={"all": "1", "filters": json.dumps({"label": labels})})
        response.raise_for_status()
        return response.json()

    async def _service_containers(self, service: str) -> list[dict]:
        return await self._list([f"com.docker.compose.project={self.project}", f"com.docker.compose.service={service}"])

    async def container(self, service: str) -> dict:
        """Inspect data of the compose service's container (a leftover `<name>-prev` never counts)."""
        containers = [c for c in await self._service_containers(service) if not _is_prev(c)]
        if not containers:
            raise DockerError(f"no container for service {service!r}")
        if len(containers) > 1:
            raise DockerError(f"{len(containers)} containers for service {service!r}; expected one")
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

    async def resume(self, service: str) -> None:
        """Unpauses or starts the container."""
        container = await self.container(service)
        if container["State"]["Paused"]:
            await self._post(f"/containers/{container['Id']}/unpause", f"unpause {service}")
        elif not container["State"]["Running"]:
            await self._post(f"/containers/{container['Id']}/start", f"start {service}")

    async def update_cpus(self, service: str, nano_cpus: int) -> None:
        container = await self.container(service)
        await self._post(f"/containers/{container['Id']}/update", f"update {service}", json={"NanoCpus": nano_cpus})

    async def wait_healthy(self, service: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = (await self.container(service))["State"]
            health = state.get("Health", {}).get("Status")
            if state["Running"] and not state["Paused"] and health in (None, "healthy"):
                return
            if time.monotonic() > deadline:
                raise DockerError(f"{service} not healthy after {timeout}s (health={health})")
            await asyncio.sleep(1)

    async def ip(self, service: str) -> str | None:
        """The service's address on its (single) compose network; None when it isn't running."""
        container = await self.container(service)
        if not container["State"]["Running"]:
            return None
        addresses = [n["IPAddress"] for n in container["NetworkSettings"]["Networks"].values() if n.get("IPAddress")]
        return addresses[0] if addresses else None

    async def self_image(self) -> str:
        """Image of this injector's own container (the helper image: it ships tc and iptables)."""
        if self._self_image is None:
            response = await self._client.get(f"/containers/{os.environ['HOSTNAME']}/json")
            response.raise_for_status()
            self._self_image = response.json()["Config"]["Image"]  # the tag, see `recreate`
        return self._self_image

    async def run_helper(self, container_id: str, script: str, timeout: float = 20) -> None:
        """Runs `script` in a throwaway container sharing the target's network namespace (NET_ADMIN).

        The helper has no compose labels, so log shipping ignores it; the rules it installs outlive it. Its own
        label lets `sweep` remove it if the injector dies before the `finally` below.
        """
        created = await self._post("/containers/create", "create helper", json={
            "Image": await self.self_image(), "Cmd": ["sh", "-c", script], "Labels": {HELPER_LABEL: "1"},
            "HostConfig": {"NetworkMode": f"container:{container_id}", "CapAdd": ["NET_ADMIN"]},
        })
        helper = created.json()["Id"]
        try:
            await self._post(f"/containers/{helper}/start", "start helper")
            waited = await self._post(f"/containers/{helper}/wait", "wait helper", timeout=timeout)
            code = waited.json()["StatusCode"]
            if code != 0:
                logs = await self._client.get(f"/containers/{helper}/logs", params={"stdout": "1", "stderr": "1"})
                raise DockerError(f"helper exited {code}: {logs.content[-500:]!r}")
        finally:
            await self._client.delete(f"/containers/{helper}", params={"force": "1"})

    async def recreate(self, container: dict, env: list[str], cmd: list[str], labels: dict[str, str],
                       restart: dict) -> None:
        """Replaces the container with one that differs only in env, command and labels (a redeploy).

        Same name, image, host config and network aliases, so DNS, compose and log shipping see the same
        service; the new container gets a new IP, which nginx re-resolves (see architecture.md).
        """
        config = container["Config"]
        body = {key: config[key] for key in ("Entrypoint", "WorkingDir", "User", "Healthcheck", "ExposedPorts",
                                             "StopSignal", "Tty", "OpenStdin") if config.get(key) is not None}
        # Config.Image (the tag compose built), not the top-level digest: with the containerd image store the
        # digest isn't accepted by /containers/create.
        body.update(Image=config["Image"], Env=env, Cmd=cmd, Labels=labels,
                    HostConfig={**container["HostConfig"], "RestartPolicy": restart})
        body["NetworkingConfig"] = {"EndpointsConfig": {
            name: {"Aliases": network.get("Aliases") or []}
            for name, network in container["NetworkSettings"]["Networks"].items()
        }}
        name, old = container["Name"].lstrip("/"), container["Id"]
        was_running = container["State"]["Running"]
        if was_running:
            await self._post(f"/containers/{old}/stop", f"stop {name}", params={"t": "2"})
        # The old container steps aside instead of being removed first, so a failed create can be rolled back.
        await self._post(f"/containers/{old}/rename", f"rename {name}", params={"name": f"{name}{PREV_SUFFIX}"})
        try:
            created = await self._post("/containers/create", f"create {name}", params={"name": name}, json=body)
        except DockerError:
            await self._post(f"/containers/{old}/rename", f"rename back {name}", params={"name": name})
            if was_running:
                await self._post(f"/containers/{old}/start", f"restart {name}")
            raise
        new = created.json()["Id"]
        try:
            await self._post(f"/containers/{new}/start", f"start {name}")
        except DockerError:  # the old container is still there: put it back
            await self._client.delete(f"/containers/{new}", params={"force": "1"})
            await self._post(f"/containers/{old}/rename", f"rename back {name}", params={"name": name})
            if was_running:
                await self._post(f"/containers/{old}/start", f"restart {name}")
            raise
        removed = await self._client.delete(f"/containers/{old}", params={"force": "1"})
        if removed.status_code not in (204, 404):  # `container()` ignores it; `sweep` removes it later
            logger.warning("prev_remove_failed", extra={"target": name, "error": f"HTTP {removed.status_code}"})

    async def sweep(self) -> None:
        """Cleans up after an injector that died mid-operation: helper containers and `<name>-prev` leftovers.

        A `-prev` container whose service has no other container was interrupted between rename and start of
        the new release, so it is renamed back and started (the periodic pass redeploys again if needed).
        """
        for helper in await self._list([HELPER_LABEL]):
            await self._client.delete(f"/containers/{helper['Id']}", params={"force": "1"})
        for prev in await self._list([f"com.docker.compose.project={self.project}"]):
            if not _is_prev(prev):
                continue
            service = prev["Labels"]["com.docker.compose.service"]
            if any(not _is_prev(c) for c in await self._service_containers(service)):
                await self._client.delete(f"/containers/{prev['Id']}", params={"force": "1"})
            else:
                name = prev["Names"][0].lstrip("/").removesuffix(PREV_SUFFIX)
                await self._post(f"/containers/{prev['Id']}/rename", f"rename back {name}", params={"name": name})
                await self._post(f"/containers/{prev['Id']}/start", f"start {name}")
            logger.warning("swept_prev_container", extra={"target": service})

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


def _is_prev(listed: dict) -> bool:
    return any(name.endswith(PREV_SUFFIX) for name in listed["Names"])
