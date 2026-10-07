"""Network faults in the target's network namespace: `tc netem` (latency, loss) and `iptables` (connection
failure), installed by a throwaway helper container (see `Docker.run_helper`). App images stay untouched.

Rules live in the namespace, so they vanish when the container restarts or is recreated. `ensure` notices
that by (container id, start time, peer IP) and re-installs them; otherwise it does nothing, which keeps the
2s reconcile pass cheap. The cache is in memory: after an injector restart the (idempotent) scripts run again.
"""

from app.docker_api import Docker

DEV = "eth0"  # every service sits on the single `backend` network
RESET = f"tc qdisc del dev {DEV} root 2>/dev/null; iptables -F OUTPUT 2>/dev/null; true"


def _netem(params: dict) -> str:
    if "delay_ms" in params:
        jitter = f" {params['jitter_ms']}ms" if params["jitter_ms"] else ""
        return f"netem delay {params['delay_ms']}ms{jitter}"
    return f"netem loss {params['loss_percent']}%"


def script(fault: dict, peer_ip: str | None) -> str:
    params = fault["parameters"]
    if fault["type"] == "connection_failure":
        action = "REJECT --reject-with tcp-reset" if params["mode"] == "reject" else "DROP"
        return f"{RESET}; iptables -A OUTPUT -d {peer_ip} -p tcp -j {action}"
    if peer_ip is None:
        return f"{RESET}; tc qdisc add dev {DEV} root {_netem(params)}"
    # prio with every priority mapped to band 1 (1:1); only the filter sends the peer's traffic to band 4.
    return (f"{RESET}; tc qdisc add dev {DEV} root handle 1: prio bands 4 priomap {' '.join(['0'] * 16)}"
            f" && tc qdisc add dev {DEV} parent 1:4 handle 40: {_netem(params)}"
            f" && tc filter add dev {DEV} parent 1:0 protocol ip prio 1 u32 match ip dst {peer_ip}/32 flowid 1:4")


class Network:
    def __init__(self, docker: Docker):
        self.docker = docker
        self._applied: dict[str, tuple] = {}

    async def ensure(self, fault: dict) -> None:
        container = await self.docker.container(fault["target"])
        state = container["State"]
        if not state["Running"] or state["Paused"]:
            return  # another fault stopped/paused it; rules are re-installed once it runs again
        peer = fault["parameters"].get("peer")
        peer_ip = await self.docker.ip(peer) if peer else None
        if peer and peer_ip is None:
            return  # the peer isn't running, so there is no address to shape yet
        key = (container["Id"], state["StartedAt"], peer_ip)
        if self._applied.get(fault["id"]) == key:
            return
        await self.docker.run_helper(container["Id"], script(fault, peer_ip))
        self._applied[fault["id"]] = key

    async def clear(self, fault: dict) -> None:
        self._applied.pop(fault["id"], None)
        container = await self.docker.container(fault["target"])
        # A paused container keeps its namespace and the rules in it; the helper joins it all the same.
        if container["State"]["Running"]:
            await self.docker.run_helper(container["Id"], RESET)
