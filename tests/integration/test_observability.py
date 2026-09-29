import uuid

import httpx
import pytest

from conftest import GRAFANA_URL, LOKI_URL, ORDER, PROMETHEUS_URL, eventually

APP_SERVICES = ["gateway", "auth", "order", "payment"]
EXPECTED_JOBS = {*APP_SERVICES, "nginx", "postgres", "redis", "prometheus"}


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


def test_request_id_generated(client):
    response = client.get("/health")
    assert response.headers.get("x-request-id")


def test_request_id_preserved(client, token):
    request_id = f"test-{uuid.uuid4().hex}"
    response = client.post(
        "/orders", json=ORDER, headers={"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
    )
    assert response.status_code == 201
    assert response.headers["x-request-id"] == request_id


@pytest.mark.parametrize("service", APP_SERVICES)
def test_metrics_endpoint(service):
    response = httpx.get(f"http://{service}:8000/metrics", timeout=5)
    assert response.status_code == 200
    assert "http_requests_total" in response.text


def test_business_counters_increase(client, token):
    before = metric_value(httpx.get("http://order:8000/metrics").text, "orders_total", status="paid")
    response = client.post("/orders", json=ORDER, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 201
    order_metrics = httpx.get("http://order:8000/metrics").text

    assert metric_value(order_metrics, "orders_total", status="paid") == before + 1
    assert metric_value(order_metrics, "http_requests_total", route="/orders", status="201") >= 1
    assert metric_value(order_metrics, "dependency_requests_total", dependency="payment", outcome="success") >= 1
    assert metric_value(order_metrics, "dependency_requests_total", dependency="postgres", outcome="success") >= 1


def test_prometheus_targets_up():
    def all_up():
        targets = httpx.get(f"{PROMETHEUS_URL}/api/v1/targets").json()["data"]["activeTargets"]
        health = {t["labels"]["job"]: t["health"] for t in targets}
        return health if set(health) == EXPECTED_JOBS and set(health.values()) == {"up"} else None

    assert eventually(all_up), httpx.get(f"{PROMETHEUS_URL}/api/v1/targets").json()


def test_request_traceable_in_loki(client, token):
    request_id = f"loki-{uuid.uuid4().hex}"
    response = client.post(
        "/orders", json=ORDER, headers={"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
    )
    assert response.status_code == 201

    expected = {"nginx", *APP_SERVICES}

    def services_with_request_id():
        result = httpx.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": f'{{service=~".+"}} |= "{request_id}"', "since": "5m", "limit": 100},
        ).json()["data"]["result"]
        found = {stream["stream"]["service"] for stream in result}
        return found if expected <= found else None

    assert eventually(services_with_request_id), f"request {request_id} not found in all services' logs"


def test_grafana_provisioned():
    datasources = {ds["uid"] for ds in httpx.get(f"{GRAFANA_URL}/api/datasources").json()}
    assert {"prometheus", "loki"} <= datasources
    for uid in ("prometheus", "loki"):
        health = httpx.get(f"{GRAFANA_URL}/api/datasources/uid/{uid}/health")
        assert health.status_code == 200, (uid, health.text)

    dashboards = {d["uid"] for d in httpx.get(f"{GRAFANA_URL}/api/search", params={"type": "dash-db"}).json()}
    assert {"sre-overview", "sre-dependencies", "sre-logs"} <= dashboards
