"""M9: every fault type in the catalog is reversible. Inject (typical parameters), remove all faults, and the
system must be back to its pre-fault state: orders 201 at baseline latency, the same releases running, the same
token TTL. One case per type; the most invasive target where a type has several (crash loop, all egress shaped).
"""

import re
import statistics

import httpx
import pytest

from conftest import DEMO_USER, eventually, inject, place_order, status_becomes

pytestmark = pytest.mark.usefixtures("recover_after")

APP_SERVICES = ("gateway", "auth", "order", "payment")

CASES = [
    ("payment_latency", "payment", {"latency_ms": 1500}),
    ("payment_error", "payment", {}),
    ("service_unavailable", "order", {}),
    ("cpu_saturation", "order", {"workers": 4}),
    ("cpu_limit", "order", {"cpus": 0.05}),
    ("memory_pressure", "order", {"mb": 200}),
    ("db_slow_query", "postgres", {"delay_ms": 500}),
    ("db_connection_exhaustion", "postgres", {}),
    ("db_lock_contention", "postgres", {}),
    ("redis_latency", "redis", {"pause_ms": 500}),
    ("redis_unavailable", "redis", {}),
    ("dependency_timeout", "payment", {}),
    ("intermittent_errors", "order", {}),
    ("network_latency", "order", {"delay_ms": 200}),  # no peer: all egress, scrapes and OTLP included
    ("packet_loss", "order", {"loss_percent": 20, "peer": "payment"}),
    ("connection_failure", "order", {"peer": "payment", "mode": "drop"}),
    ("incorrect_endpoint", "order", {"dependency": "payment"}),
    ("incorrect_timeout", "gateway", {"timeout_ms": 5}),
    ("bad_configuration", "auth", {"settings": {"TOKEN_TTL_SECONDS": 2}}),
    ("bad_deployment", "order", {"defect": "crash"}),
    ("proxy_rate_limit", "nginx", {"rate_rps": 1, "burst": 0}),
    ("proxy_bandwidth_limit", "nginx", {}),
]


def releases() -> dict[str, str | None]:
    found = {}
    for service in APP_SERVICES:
        try:
            match = re.search(r'app_build_info\{version="([^"]+)"', httpx.get(f"http://{service}:8000/metrics").text)
        except httpx.HTTPError:
            match = None
        found[service] = match and match.group(1)
    return found


def token_ttl(client) -> int:
    return client.post("/login", json=DEMO_USER).json()["expires_in"]


def order_latency(client, token, n: int = 5) -> tuple[list[int], float]:
    results = [place_order(client, token) for _ in range(n)]
    return [r.status_code for r, _ in results], statistics.median(elapsed for _, elapsed in results)


@pytest.fixture(scope="module")
def baseline(client):
    token = client.post("/login", json=DEMO_USER).json()["access_token"]
    statuses, latency = order_latency(client, token)
    assert statuses == [201] * 5, statuses
    return {"latency": latency, "releases": releases(), "ttl": token_ttl(client)}


def test_catalog_is_covered():
    """A new fault type must be added here too; the list mirrors services/fault-injector/app/catalog.py."""
    assert len({type_ for type_, _, _ in CASES}) == len(CASES) == 22


@pytest.mark.parametrize(("type_", "target", "parameters"), CASES, ids=[c[0] for c in CASES])
def test_recovers_after_removal(faults, client, token, baseline, type_, target, parameters):
    fault = inject(faults, type_, target, **parameters)
    assert fault["state"] == "active", fault

    response = faults.delete("/faults")
    assert response.status_code == 200, response.text
    assert faults.get("/faults", params={"state": "active"}).json() == []

    assert eventually(lambda: releases() == baseline["releases"], timeout=60), (releases(), baseline["releases"])
    assert status_becomes(client, token, 201, timeout=60), f"orders not 201 after removing {type_}"
    statuses, latency = order_latency(client, token)
    assert statuses == [201] * 5, statuses
    assert latency < baseline["latency"] + 0.15, (latency, baseline["latency"])
    assert token_ttl(client) == baseline["ttl"]
