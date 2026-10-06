"""cpu_limit: the target's CPU quota lowered in place, like `docker update --cpus 0.1` by a misconfigured
rollout or an autoscaler. The process keeps running and isn't restarted; it simply gets less CPU, so its
throughput drops by a stable factor (unlike CPU hogs competing for the same quota, which swing run to run).

The quota compose gave the container is saved before the first change and restored on revert. If compose
recreates the container (`make up`), it comes back with the compose quota and `ensure` lowers it again.
"""

from app.docker_api import Docker
from app.store import FaultStore


class CpuLimits:
    def __init__(self, docker: Docker, store: FaultStore):
        self.docker = docker
        self.store = store

    async def ensure(self, fault: dict) -> None:
        service, wanted = fault["target"], round(fault["parameters"]["cpus"] * 1e9)
        current = (await self.docker.container(service))["HostConfig"].get("NanoCpus") or 0
        if current == wanted:
            return
        self.store.save_cpu_baseline(service, current)  # kept if already saved (compose recreated the container)
        await self.docker.update_cpus(service, wanted)

    async def restore(self, service: str) -> None:
        baseline = self.store.cpu_baseline(service)
        if baseline is not None:
            await self.docker.update_cpus(service, baseline)
        self.store.delete_cpu_baseline(service)
