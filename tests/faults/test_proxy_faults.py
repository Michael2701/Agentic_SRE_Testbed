"""M8: faults in the proxy domain (nginx) and the harmless release used as a decoy.

The proxy has no latency metrics in Prometheus: the app services' own telemetry stays normal, only the
client (and nginx logs/traces) see the delay.
"""

import concurrent.futures
import time

import httpx
import pytest

from conftest import ORDER_METRICS, eventually, inject, metric_value, place_order

pytestmark = pytest.mark.usefixtures("recover_after")

GATEWAY_METRICS = "http://gateway:8000/metrics"


def gateway_orders_seconds() -> float:
    return metric_value(httpx.get(GATEWAY_METRICS).text, "http_request_duration_seconds_sum",
                        method="POST", route="/orders")


def test_proxy_network_latency_invisible_to_the_app(faults, client, token):
    inject(faults, "network_latency", "nginx", delay_ms=300, peer="gateway")
    before = gateway_orders_seconds()
    results = [place_order(client, token) for _ in range(3)]
    assert all(r.status_code == 201 and elapsed >= 0.3 for r, elapsed in results), results
    assert gateway_orders_seconds() - before < 0.3  # the gateway itself served them fast


def test_proxy_bandwidth_limit(faults, client, token):
    fault = inject(faults, "proxy_bandwidth_limit", bytes_per_second=100)
    before = gateway_orders_seconds()
    response, elapsed = place_order(client, token)
    assert response.status_code == 201 and elapsed >= 1  # ~300 bytes at 100 B/s
    assert gateway_orders_seconds() - before < 0.1

    faults.delete(f"/faults/{fault['id']}")  # snippet emptied + nginx reload
    response, elapsed = place_order(client, token)
    assert response.status_code == 201 and elapsed < 0.5


def test_proxy_rate_limit(faults, client, token):
    inject(faults, "proxy_rate_limit", rate_rps=2, burst=2)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=client.base_url, timeout=30) as burst_client:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            started = time.monotonic()
            statuses = list(pool.map(lambda _: burst_client.get("/orders/00000000-0000-0000-0000-000000000000",
                                                                headers=headers).status_code, range(8)))
            elapsed = time.monotonic() - started
    assert statuses.count(503) >= 3, statuses  # over rate + burst: rejected by the proxy
    assert elapsed >= 0.4  # the burst was delayed, not served at once


def test_harmless_deployment(faults, client, token):
    started = metric_value(httpx.get(ORDER_METRICS).text, "process_start_time_seconds")
    inject(faults, "bad_deployment", "order", version="1.1.0", defect="none")
    assert metric_value(httpx.get(ORDER_METRICS).text, "app_build_info", version="1.1.0") == 1
    assert metric_value(httpx.get(ORDER_METRICS).text, "process_start_time_seconds") > started
    assert eventually(lambda: place_order(client, token)[0].status_code == 201, timeout=20)
    results = [place_order(client, token) for _ in range(3)]
    assert all(r.status_code == 201 and elapsed < 0.5 for r, elapsed in results)  # a new version, no harm


@pytest.mark.parametrize("body", [
    {"type": "proxy_rate_limit", "target": "gateway"},
    {"type": "proxy_bandwidth_limit", "parameters": {"bytes_per_second": 0}},
    {"type": "network_latency", "target": "nginx", "parameters": {"delay_ms": 10, "peer": "nginx"}},
])
def test_invalid_proxy_faults_rejected(faults, body):
    assert faults.post("/faults", json=body).status_code == 422
