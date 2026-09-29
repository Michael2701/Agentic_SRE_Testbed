"""Fault injection tests: the API, an observable incident per fault type, recovery, and control-plane isolation.

Every test leaves the system recovered (autouse fixture), because faults change shared state.
"""

import time
import uuid

import httpx
import pytest

from conftest import (
    ORDER_METRICS, PAYMENT_METRICS, PROMETHEUS_URL, TEMPO_URL, eventually, inject, loki_streams, metric_value,
    place_order,
)

pytestmark = pytest.mark.usefixtures("recover_after")


# ---------------------------------------------------------------- API


def test_fault_lifecycle(faults):
    fault = inject(faults, "payment_latency", latency_ms=100)
    assert fault["id"].startswith("flt-")
    assert fault["experiment_id"] == "test-faults"
    assert fault["type"] == "payment_latency"
    assert fault["target"] == "payment"
    assert fault["parameters"] == {"latency_ms": 100, "jitter_ms": 0, "probability": 1.0}
    assert fault["state"] == "active"
    assert fault["created_at"]

    assert faults.get(f"/faults/{fault['id']}").json()["state"] == "active"
    assert fault["id"] in {f["id"] for f in faults.get("/faults", params={"state": "active"}).json()}

    duplicate = faults.post("/faults", json={"type": "payment_latency", "parameters": {"latency_ms": 5}})
    assert duplicate.status_code == 409

    removed = faults.delete(f"/faults/{fault['id']}").json()
    assert removed["state"] == "removed"
    assert fault["id"] not in {f["id"] for f in faults.get("/faults", params={"state": "active"}).json()}
    assert fault["id"] in {f["id"] for f in faults.get("/faults", params={"state": "removed"}).json()}
    assert faults.get("/faults/flt-missing").status_code == 404


def test_experiment_id_generated_when_missing(faults):
    response = faults.post("/faults", json={"type": "payment_error"})
    assert response.status_code == 201
    assert response.json()["experiment_id"].startswith("adhoc-")


@pytest.mark.parametrize("body", [
    {"type": "cosmic_rays"},
    {"type": "payment_latency", "parameters": {}},
    {"type": "payment_latency", "parameters": {"latency_ms": 0}},
    {"type": "payment_latency", "parameters": {"latency_ms": 100, "unknown": 1}},
    {"type": "payment_latency", "target": "auth", "parameters": {"latency_ms": 100}},
    {"type": "payment_error", "parameters": {"status_code": 404}},
    {"type": "service_unavailable"},
    {"type": "service_unavailable", "target": "postgres"},
])
def test_invalid_faults_rejected(faults, body):
    assert faults.post("/faults", json=body).status_code == 422


def test_delete_all(faults):
    inject(faults, "payment_latency", latency_ms=50)
    inject(faults, "payment_error", probability=0.5)
    removed = faults.delete("/faults").json()
    assert {f["type"] for f in removed} == {"payment_latency", "payment_error"}
    assert all(f["state"] == "removed" for f in removed)
    assert faults.get("/faults", params={"state": "active"}).json() == []


# ---------------------------------------------------------------- Incidents


def test_payment_latency_incident(faults, client, token):
    def payment_seconds():
        text = httpx.get(ORDER_METRICS).text
        return metric_value(text, "dependency_request_duration_seconds_sum", dependency="payment")

    before = payment_seconds()
    fault = inject(faults, "payment_latency", latency_ms=1500)

    response, elapsed = place_order(client, token)
    assert response.status_code == 201
    assert elapsed >= 1.5
    assert payment_seconds() - before >= 1.5  # visible in order's dependency latency

    faults.delete(f"/faults/{fault['id']}")
    response, elapsed = place_order(client, token)
    assert response.status_code == 201
    assert elapsed < 1.0


def test_payment_error_incident(faults, client, token):
    before = metric_value(httpx.get(ORDER_METRICS).text, "orders_total", status="payment_failed")
    fault = inject(faults, "payment_error", status_code=503)

    request_id = f"fault-{uuid.uuid4().hex}"
    response, _ = place_order(client, token, request_id)
    assert response.status_code == 502
    assert response.json()["order"]["status"] == "payment_failed"
    assert metric_value(httpx.get(ORDER_METRICS).text, "orders_total", status="payment_failed") == before + 1

    def error_logs():
        streams = loki_streams(f'{{service=~"order|payment", level=~"warning|error"}} |= "{request_id}"')
        found = {s["stream"]["service"] for s in streams}
        return found if {"order", "payment"} <= found else None

    assert eventually(error_logs), "payment failure not visible in logs"

    faults.delete(f"/faults/{fault['id']}")
    assert place_order(client, token)[0].status_code == 201


@pytest.mark.parametrize("target", ["payment", "auth", "order"])
def test_service_unavailable_incident(faults, client, token, target):
    inject(faults, "service_unavailable", target=target)

    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://{target}:8000/health", timeout=2)
    response, _ = place_order(client, token)
    assert response.status_code == 502, response.text

    if target == "payment":
        text = httpx.get(ORDER_METRICS).text
        assert metric_value(text, "dependency_requests_total", dependency="payment", outcome="error") >= 1

    removed = faults.delete("/faults").json()
    assert [f["state"] for f in removed] == ["removed"]
    assert httpx.get(f"http://{target}:8000/health", timeout=2).status_code == 200
    assert place_order(client, token)[0].status_code == 201


def test_reconcile_restores_lost_payment_config(faults):
    """A payment restart loses its in-memory config; the reconcile loop pushes it again."""
    inject(faults, "payment_latency", latency_ms=10)
    httpx.delete("http://payment:8000/__faults")
    assert eventually(lambda: httpx.get("http://payment:8000/__faults").json()["latency_ms"] == 10, timeout=10)


# ---------------------------------------------------------------- Control-plane isolation


def test_control_plane_invisible_to_diagnostic_plane(faults, client, token):
    inject(faults, "payment_latency", latency_ms=20)
    place_order(client, token)
    time.sleep(3)  # let a reconcile pass and log shipping happen

    assert loki_streams('{service="fault-injector"}', since="1h") == []
    assert loki_streams('{service=~".+"} |= "__faults"', since="1h") == []

    jobs = {t["labels"]["job"] for t in httpx.get(f"{PROMETHEUS_URL}/api/v1/targets").json()["data"]["activeTargets"]}
    assert not any("fault" in job for job in jobs)
    assert "__faults" not in httpx.get(PAYMENT_METRICS).text

    traces = httpx.get(f"{TEMPO_URL}/api/search", params={"q": '{ name =~ ".*__faults.*" }', "limit": 5}).json()
    assert traces.get("traces", []) == []
