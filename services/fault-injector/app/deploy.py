"""Configuration faults as real (re)deployments: the target container is recreated with a changed env or
command, exactly like rolling out a new release or config. The process restarts and reads the new values.

The env/command compose gave the container is saved (the baseline) before the first redeploy and restored
on revert. A container without our label was created by compose (first redeploy, or compose recreated it,
e.g. `make up` after a rebuild), so its current config is the baseline. The label value is a digest of the
fault id: neutral, and it tells `ensure` whether the container already runs the desired release.

A crashing release gets restart policy on-failure (a crash loop, like CrashLoopBackOff): a container that
exits for good drops out of log shipping (Alloy discovers running/restarting containers only), so its crash
logs would never reach Loki.
"""

import hashlib

from app.catalog import BROKEN_APP_SPEC, deployment
from app.docker_api import Docker, DockerError
from app.store import FaultStore

LABEL = "io.testbed.rev"
APP_SPEC = "app.main:app"
CRASH_LOOP = {"Name": "on-failure", "MaximumRetryCount": 0}


def _revision(fault_id: str) -> str:
    return hashlib.sha256(fault_id.encode()).hexdigest()[:12]


def _merge(env: list[str], overrides: dict[str, str]) -> list[str]:
    kept = [entry for entry in env if entry.split("=", 1)[0] not in overrides]
    return kept + [f"{key}={value}" for key, value in overrides.items()]


class Deployer:
    def __init__(self, docker: Docker, store: FaultStore):
        self.docker = docker
        self.store = store

    async def ensure(self, fault: dict) -> bool:
        """Idempotent; returns True when the container was recreated just now."""
        service = fault["target"]
        container = await self.docker.container(service)
        labels = container["Config"]["Labels"] or {}
        revision = _revision(fault["id"])
        if labels.get(LABEL) == revision:
            return False
        if LABEL not in labels:
            self.store.save_baseline(service, container["Config"]["Env"] or [], container["Config"]["Cmd"] or [],
                                     container["HostConfig"].get("RestartPolicy") or {"Name": "no"})
        baseline = self.store.baseline(service)
        if baseline is None:
            raise DockerError(f"no saved baseline for {service}; recreate it with `make up`")
        env, cmd, restart = baseline
        overrides, broken = deployment(fault, dict(entry.split("=", 1) for entry in env if "=" in entry))
        if broken:
            cmd, restart = [BROKEN_APP_SPEC if part == APP_SPEC else part for part in cmd], CRASH_LOOP
        await self.docker.recreate(container, _merge(env, overrides), cmd, {**labels, LABEL: revision}, restart)
        return True

    async def restore(self, service: str) -> None:
        """Redeploys the baseline if a changed release is running (the caller waits for it to be healthy)."""
        container = await self.docker.container(service)
        labels = dict(container["Config"]["Labels"] or {})
        if labels.pop(LABEL, None) is not None:
            baseline = self.store.baseline(service)
            if baseline is None:
                raise DockerError(f"no saved baseline to restore {service}; recreate it with `make up`")
            env, cmd, restart = baseline
            await self.docker.recreate(container, env, cmd, labels, restart)
        self.store.delete_baseline(service)
