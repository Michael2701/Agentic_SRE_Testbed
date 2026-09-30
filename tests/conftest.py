import os
import statistics
import time

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")
TEMPO_URL = os.environ.get("TEMPO_URL", "http://localhost:3200")
FAULT_INJECTOR_URL = os.environ.get("FAULT_INJECTOR_URL", "http://localhost:8090")
EXPERIMENT_RUNNER_URL = os.environ.get("EXPERIMENT_RUNNER_URL", "http://localhost:8091")
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
        if not (line.startswith(name + "{") or line.startswith(name + " ")):  # labelled or unlabelled sample
            continue
        series, value = line.rsplit(" ", 1)
        if all(f'{key}="{val}"' in series for key, val in labels.items()):
            total += float(value)
    return total


def loki_streams(query: str, since: str = "5m") -> list[dict]:
    return httpx.get(
        f"{LOKI_URL}/loki/api/v1/query_range", params={"query": query, "since": since, "limit": 100}
    ).json()["data"]["result"]


# ---------------------------------------------------------------- fault-test helpers

ORDER_METRICS = "http://order:8000/metrics"
PAYMENT_METRICS = "http://payment:8000/metrics"
AUTH_METRICS = "http://auth:8000/metrics"


def inject(faults, type_: str, target: str | None = None, **parameters) -> dict:
    body = {"type": type_, "parameters": parameters, "experiment_id": "test-faults"}
    if target:
        body["target"] = target
    response = faults.post("/faults", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def place_order(client, token, request_id: str | None = None) -> tuple[httpx.Response, float]:
    headers = {"Authorization": f"Bearer {token}"}
    if request_id:
        headers["X-Request-ID"] = request_id
    start = time.monotonic()
    response = client.post("/orders", json=ORDER, headers=headers, timeout=30)
    return response, time.monotonic() - start


def sample(client, token, n: int, spacing: float = 0.25) -> list[tuple[int, float]]:
    """Requests spread over time (periodic faults hold resources only part of the time)."""
    results = []
    for _ in range(n):
        response, elapsed = place_order(client, token)
        results.append((response.status_code, elapsed))
        time.sleep(spacing)
    return results


def median_latency(results) -> float:
    return statistics.median(elapsed for _, elapsed in results)


def five_xx_ratio(results) -> float:
    return sum(status >= 500 for status, _ in results) / len(results)


def prom_value(query: str) -> float:
    result = httpx.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}).json()["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


@pytest.fixture(scope="session")
def faults():
    with httpx.Client(base_url=FAULT_INJECTOR_URL, timeout=90) as client:
        yield client


@pytest.fixture
def recover_after(faults, client):
    """Fault tests use this (via pytestmark): remove all faults and wait until the system is healthy."""
    yield
    response = faults.delete("/faults")
    assert response.status_code == 200, response.text

    def healthy():
        try:
            payment_ok = httpx.get("http://payment:8000/health", timeout=2).status_code == 200
            return payment_ok and client.get("/ready").status_code == 200
        except httpx.HTTPError:
            return False

    assert eventually(healthy, timeout=60), "system did not recover after removing faults"


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
