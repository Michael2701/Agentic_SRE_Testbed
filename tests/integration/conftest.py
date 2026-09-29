import os
import time

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")
TEMPO_URL = os.environ.get("TEMPO_URL", "http://localhost:3200")
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
