"""Is the testbed alive and clean? Run from the host after `make up` / `make reset` (stdlib only, ~5 s).

Checks the public path (ready, login, one order), the control plane (no active faults, no leftover
`*-prev` containers of an interrupted redeploy) and the telemetry backends (scrape targets, Loki, Tempo).
Exit code 1 on any failure.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

APP = f"http://localhost:{os.environ.get('NGINX_PORT', '8080')}"
FAULTS = f"http://localhost:{os.environ.get('FAULT_INJECTOR_PORT', '8090')}"
PROMETHEUS = f"http://localhost:{os.environ.get('PROMETHEUS_PORT', '9090')}"
GRAFANA = f"http://localhost:{os.environ.get('GRAFANA_PORT', '3000')}"
PORTAL = f"http://localhost:{os.environ.get('PORTAL_PORT', '8000')}"


def call(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, str]:
    headers = {"content-type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except OSError as exc:
        return 0, repr(exc)


def check_app() -> str | None:
    status, _ = call(f"{APP}/ready")
    if status != 200:
        return f"/ready {status}"
    status, body = call(f"{APP}/login", {"username": "alice", "password": "alice"})
    if status != 200:
        return f"login {status}"
    status, body = call(f"{APP}/orders", {"item": "book", "quantity": 1, "amount_cents": 1500},
                        json.loads(body)["access_token"])
    return None if status == 201 else f"order {status}: {body[:200]}"


def check_faults() -> str | None:
    status, body = call(f"{FAULTS}/faults?state=active")
    if status != 200:
        return f"fault-injector {status}"
    active = json.loads(body)
    return f"{len(active)} active: {[f['type'] for f in active]} (make recover)" if active else None


def check_containers() -> str | None:
    names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                           capture_output=True, text=True, check=True).stdout.split()
    leftovers = [name for name in names if name.startswith("sre-testbed-") and name.endswith("-prev")]
    return f"leftover containers {leftovers}" if leftovers else None


def check_targets() -> str | None:
    status, body = call(f"{PROMETHEUS}/api/v1/targets")
    if status != 200:
        return f"prometheus {status}"
    down = [t["labels"]["job"] for t in json.loads(body)["data"]["activeTargets"] if t["health"] != "up"]
    return f"targets down: {down}" if down else None


def check_loki() -> str | None:
    status, body = call(f"{GRAFANA}/api/datasources/uid/loki/health")
    return None if status == 200 else f"loki {status}: {body[:200]}"


def check_tempo() -> str | None:
    status, body = call(f"{GRAFANA}/api/datasources/proxy/uid/tempo/api/echo")
    return None if status == 200 else f"tempo {status}: {body[:200]}"


def check_portal() -> str | None:
    paths = ["/", "/grafana/api/health", "/prometheus/-/ready", "/faults/health", "/experiments/health",
             "/app/nginx-health"]
    failed = [f"{path} {status}" for path in paths if (status := call(PORTAL + path)[0]) != 200]
    return ", ".join(failed) if failed else None


CHECKS = [("app: ready, login, order 201", check_app), ("no active faults", check_faults),
          ("no leftover redeploy containers", check_containers), ("prometheus targets up", check_targets),
          ("loki reachable", check_loki), ("tempo reachable", check_tempo),
          (f"portal {PORTAL}: every tool reachable", check_portal)]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        error = check()
        failed += error is not None
        print(f"{'ok  ' if error is None else 'FAIL'} {name}{'' if error is None else ': ' + error}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
