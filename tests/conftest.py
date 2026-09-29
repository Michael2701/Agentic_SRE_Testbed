import os
import time

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")
TEMPO_URL = os.environ.get("TEMPO_URL", "http://localhost:3200")
FAULT_INJECTOR_URL = os.environ.get("FAULT_INJECTOR_URL", "http://localhost:8090")
READY_TIMEOUT_SECONDS = float(os.environ.get("READY_TIMEOUT_SECONDS", "60"))
DEMO_USER = {"username": "alice", "password": "alice"}
ORDER = {"item": "book", "quantity": 1, "amount_cents": 1500}


def eventually(check, timeout=45, interval=1):
    """Retries `check` until it returns a truthy value; telemetry pipelines are asynchronous."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result or time.monotonic() > deadline:
            return result
        time.sleep(interval)


def metric_value(text: str, name: str, **labels) -> float:
    """Sums samples of `name` whose labels include `labels` in Prometheus text exposition."""
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        series, value = line.rsplit(" ", 1)
        if all(f'{key}="{val}"' in series for key, val in labels.items()):
            total += float(value)
    return total


def loki_streams(query: str, since: str = "5m") -> list[dict]:
    return httpx.get(
        f"{LOKI_URL}/loki/api/v1/query_range", params={"query": query, "since": since, "limit": 100}
    ).json()["data"]["result"]


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            try:
                if client.get("/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"environment at {BASE_URL} not ready after {READY_TIMEOUT_SECONDS}s")
            time.sleep(1)
        yield client


@pytest.fixture
def token(client):
    response = client.post("/login", json=DEMO_USER)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]
