"""M5 DoD: different root causes produce similar external symptoms, yet each leaves its own evidence.

Two symptom groups on `POST /orders`: "slow but successful" and "5xx". For each root cause we check the
symptom, the recovery, and (separately) the diagnostic evidence it leaves in telemetry.
"""

import statistics
import time

import httpx
import pytest

from conftest import (
    AUTH_METRICS, ORDER_METRICS, PROMETHEUS_URL, eventually, inject, loki_streams, metric_value, place_order,
)

pytestmark = pytest.mark.usefixtures("recover_after")


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


# ---------------------------------------------------------------- symptom: slow but successful

SLOW_CAUSES = [
    pytest.param("payment_latency", None, {"latency_ms": 400}, id="payment_latency"),
    pytest.param("db_slow_query", None, {"delay_ms": 200}, id="db_slow_query"),
    pytest.param("db_lock_contention", None, {"hold_ms": 1500, "interval_ms": 2000}, id="db_lock_contention"),
    pytest.param("redis_latency", None, {"pause_ms": 300, "interval_ms": 400}, id="redis_latency"),
    pytest.param("cpu_saturation", "order", {"workers": 6}, id="cpu_saturation_order"),
]


@pytest.mark.parametrize("fault_type,target,params", SLOW_CAUSES)
def test_slow_orders_symptom(faults, client, token, fault_type, target, params):
    baseline = median_latency(sample(client, token, 6))
    fault = inject(faults, fault_type, target, **params)
    time.sleep(2)

    during = sample(client, token, 8)
    assert all(status == 201 for status, _ in during), during
    assert median_latency(during) >= max(2 * baseline, baseline + 0.03), (baseline, during)

    faults.delete(f"/faults/{fault['id']}")
    time.sleep(1)
    assert median_latency(sample(client, token, 6)) < 2 * baseline + 0.03


# ---------------------------------------------------------------- symptom: 5xx

ERROR_CAUSES = [
    pytest.param("payment_error", None, {}, 8, (0.9, 1.0), id="payment_error"),
    pytest.param("intermittent_errors", "payment", {"error_rate": 0.5}, 16, (0.1, 0.9), id="intermittent_errors"),
    pytest.param("db_connection_exhaustion", None, {}, 8, (0.9, 1.0), id="db_connection_exhaustion"),
    pytest.param("redis_unavailable", None, {}, 8, (0.9, 1.0), id="redis_unavailable"),
    pytest.param("dependency_timeout", "payment", {}, 2, (0.9, 1.0), id="dependency_timeout_payment"),
]


@pytest.mark.parametrize("fault_type,target,params,n,ratio", ERROR_CAUSES)
def test_failing_orders_symptom(faults, client, token, fault_type, target, params, n, ratio):
    fault = inject(faults, fault_type, target, **params)
    time.sleep(2)

    during = sample(client, token, n, spacing=0.1)
    low, high = ratio
    assert low <= five_xx_ratio(during) <= high, during

    faults.delete(f"/faults/{fault['id']}")
    after = sample(client, token, 4, spacing=0.1)
    assert all(status == 201 for status, _ in after), after


# ---------------------------------------------------------------- evidence per root cause


def test_cpu_saturation_evidence(faults):
    def throttled():
        return metric_value(httpx.get(ORDER_METRICS).text, "container_cpu_throttled_seconds_total")

    before = throttled()
    inject(faults, "cpu_saturation", "order", workers=4)
    assert eventually(lambda: throttled() - before > 1.0, timeout=20), "no CPU throttling visible"


def test_memory_pressure_evidence(faults):
    def usage():
        return metric_value(httpx.get(ORDER_METRICS).text, "container_memory_usage_bytes")

    before = usage()
    inject(faults, "memory_pressure", "order", mb=150)
    assert eventually(lambda: usage() - before > 120 * 2**20, timeout=20), "memory increase not visible"


def test_db_lock_contention_evidence(faults, client, token):
    inject(faults, "db_lock_contention", hold_ms=1500, interval_ms=2000)
    sample(client, token, 4)  # writers queue behind the lock
    assert eventually(lambda: prom_value('max_over_time(pg_locks_count{datname="orders",mode="sharelock"}[1m])') > 0,
                      timeout=30), "ShareLock not visible in pg_locks"


def test_db_connection_exhaustion_evidence(faults):
    inject(faults, "db_connection_exhaustion")
    assert eventually(lambda: prom_value('sum(pg_stat_activity_count{datname="orders",usename="reporting"})') >= 30,
                      timeout=30), "held connections not visible in pg_stat_activity"


def test_redis_latency_evidence(faults, client, token):
    def redis_seconds():
        return metric_value(httpx.get(AUTH_METRICS).text, "dependency_request_duration_seconds_sum", dependency="redis")

    before = redis_seconds()
    inject(faults, "redis_latency", pause_ms=300, interval_ms=400)
    sample(client, token, 6)
    assert redis_seconds() - before > 0.3  # auth's client-side view of Redis


def test_dependency_timeout_evidence(faults, client, token):
    def timeouts():
        return metric_value(httpx.get(ORDER_METRICS).text, "dependency_requests_total",
                            dependency="payment", outcome="timeout")

    before = timeouts()
    inject(faults, "dependency_timeout", "payment")
    response, elapsed = place_order(client, token)
    assert response.status_code >= 500 and elapsed >= 4.5
    assert timeouts() == before + 1  # a hang (timeout), unlike service_unavailable's connection error


# ---------------------------------------------------------------- API rules for M5 types


@pytest.mark.parametrize("body", [
    {"type": "db_lock_contention", "parameters": {"hold_ms": 2000, "interval_ms": 2000}},
    {"type": "redis_latency", "parameters": {"pause_ms": 500, "interval_ms": 400}},
    {"type": "cpu_saturation", "target": "postgres"},
    {"type": "memory_pressure", "target": "order", "parameters": {"mb": 1}},
    {"type": "db_slow_query", "parameters": {"delay_ms": 100, "operations": ["delete"]}},
    {"type": "intermittent_errors", "target": "gateway"},
])
def test_invalid_advanced_faults_rejected(faults, body):
    assert faults.post("/faults", json=body).status_code == 422


def test_container_state_conflict(faults):
    inject(faults, "dependency_timeout", "auth")
    clash = faults.post("/faults", json={"type": "service_unavailable", "target": "auth"})
    assert clash.status_code == 409  # can't be paused and stopped at once


def test_no_mechanism_names_in_logs(faults, client, token):
    inject(faults, "db_slow_query", delay_ms=50)
    inject(faults, "cpu_saturation", "payment", workers=1)
    sample(client, token, 3)
    time.sleep(3)
    for needle in ("orders_write_hook", "/tmp/.w-", "flt-"):
        assert loki_streams(f'{{service=~".+"}} |= "{needle}"', since="1h") == [], needle
