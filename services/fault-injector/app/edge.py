"""Proxy configuration faults: nginx config snippets pushed like a config-management rollout.

nginx.conf includes `/etc/nginx/runtime/*-http.conf` (http context) and `*-location.conf` (the proxied
location). The directory is the shared volume `edgeconf`: read-only for nginx, writable here (`/edge`).
`ensure` renders the snippets for the active faults and, when they differ from what nginx last loaded, writes
them and runs `nginx -c <conf> -s reload` (a graceful reload, as operators do); a failed reload is retried on
the next pass. Empty snippets = the original behaviour.
"""

from pathlib import Path

from app.docker_api import Docker, DockerError

HTTP_FILE = "edge-http.conf"
LOCATION_FILE = "edge-location.conf"
NGINX_CONF = "/etc/nginx/testbed/nginx.conf"  # as in the nginx command in docker-compose.yml


def render(active: list[dict]) -> dict[str, str]:
    http, location = [], []
    for fault in active:
        if fault["type"] == "proxy_rate_limit":
            params = fault["parameters"]
            # $server_port is a constant, non-empty key: one shared bucket for all clients.
            http.append(f"limit_req_zone $server_port zone=edge:1m rate={params['rate_rps']}r/s;")
            http.append("limit_req_status 503;")
            burst = f" burst={params['burst']}" if params["burst"] else ""  # nginx rejects burst=0; omitted = 0
            location.append(f"limit_req zone=edge{burst};")
        elif fault["type"] == "proxy_bandwidth_limit":
            location.append(f"limit_rate {fault['parameters']['bytes_per_second']};")
    return {HTTP_FILE: "\n".join(http) + "\n" if http else "", LOCATION_FILE: "\n".join(location) + "\n" if location else ""}


class Edge:
    def __init__(self, docker: Docker, directory: str):
        self.docker = docker
        self.dir = Path(directory)
        self._loaded: dict[str, str] | None = None  # snippets nginx runs with; unknown after an injector restart

    async def ensure(self, active: list[dict]) -> None:
        wanted = render(active)
        if wanted == self._loaded:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        for name, content in wanted.items():
            path = self.dir / name
            if not path.exists() or path.read_text() != content:
                path.write_text(content)
        container = await self.docker.container("nginx")
        if container["State"]["Running"]:  # otherwise nginx reads the files when it starts
            code = await self.docker.exec("nginx", ["nginx", "-c", NGINX_CONF, "-s", "reload"])
            if code != 0:
                raise DockerError(f"nginx reload failed (exit {code})")
        self._loaded = wanted
