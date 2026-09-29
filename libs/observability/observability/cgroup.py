"""Container resource metrics read from the service's own cgroup v2 (whole container, all processes).

Replaces cAdvisor, which cannot map containers on Docker Desktop's containerd store. Unlike
`process_*` metrics, these include every process in the container.
"""

from pathlib import Path

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

CGROUP = Path("/sys/fs/cgroup")


def _read(name: str) -> str | None:
    try:
        return (CGROUP / name).read_text()
    except OSError:
        return None


def _keyed(name: str) -> dict[str, int]:
    text = _read(name) or ""
    return {k: int(v) for k, v in (line.split() for line in text.splitlines() if len(line.split()) == 2)}


def _pressure_total_seconds(name: str) -> float | None:
    """`some ... total=<usec>` line of a PSI file: time at least one task was stalled on the resource."""
    for line in (_read(name) or "").splitlines():
        if line.startswith("some "):
            fields = dict(part.split("=") for part in line.split()[1:])
            return int(fields["total"]) / 1e6
    return None


class CgroupCollector(Collector):
    def collect(self):
        cpu = _keyed("cpu.stat")
        if "usage_usec" in cpu:
            yield CounterMetricFamily("container_cpu_usage_seconds", "CPU time used by all processes in the container",
                                      value=cpu["usage_usec"] / 1e6)
            yield CounterMetricFamily("container_cpu_throttled_seconds", "Time the container was CPU-throttled",
                                      value=cpu.get("throttled_usec", 0) / 1e6)
            yield CounterMetricFamily("container_cpu_throttled_periods", "Scheduler periods with throttling",
                                      value=cpu.get("nr_throttled", 0))

        quota = (_read("cpu.max") or "").split()
        if len(quota) == 2 and quota[0] != "max":
            yield GaugeMetricFamily("container_cpu_limit_cores", "CPU limit in cores", value=int(quota[0]) / int(quota[1]))

        current = _read("memory.current")
        if current:
            yield GaugeMetricFamily("container_memory_usage_bytes", "Memory used by the container", value=int(current))
        limit = (_read("memory.max") or "").strip()
        if limit and limit != "max":
            yield GaugeMetricFamily("container_memory_limit_bytes", "Memory limit of the container", value=int(limit))
        oom = _keyed("memory.events").get("oom_kill")
        if oom is not None:
            yield CounterMetricFamily("container_oom_kills", "Processes killed by the OOM killer", value=oom)

        for resource in ("cpu", "memory"):
            stalled = _pressure_total_seconds(f"{resource}.pressure")
            if stalled is not None:
                yield CounterMetricFamily(f"container_{resource}_pressure_stalled_seconds",
                                          f"PSI: time some task was stalled waiting for {resource}", value=stalled)


def register_cgroup_collector(registry) -> None:
    if (CGROUP / "cpu.stat").exists():
        registry.register(CgroupCollector())
