"""M6: infrastructure faults realized with real mechanisms: tc netem / iptables in a container's network
namespace, and redeploys (the container recreated with a changed env or entrypoint).

Each test checks the external symptom, the evidence that tells this cause apart from look-alikes (see the
symptom -> cause table in docs/kb/faults.md), and, where the revert path is specific, the recovery.
"""

import time

import httpx
import pytest

from conftest import (
    DEMO_USER, ORDER_METRICS, PAYMENT_METRICS, eventually, five_xx_ratio, inject, loki_caught_up, loki_streams,
    median_latency, metric_value, place_order, prom_value, sample, status_becomes,
)

pytestmark = pytest.mark.usefixtures("recover_after")

GATEWAY_METRICS = "http://gateway:8000/metrics"


def metrics(url: str) -> str:
    return httpx.get(url, timeout=5).text


def wait_up(url: str, timeout: float = 60) -> None:
    def ok():
        try:
            return httpx.get(url, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    assert eventually(ok, timeout=timeout), f"{url} not up"


def start_time(metrics_url: str) -> float:
    return metric_value(metrics(metrics_url), "process_start_time_seconds")


# ---------------------------------------------------------------- network namespace (tc / iptables)


def test_network_latency_to_one_peer(faults, client, token):
    def dependency_seconds(dependency):
        return metric_value(metrics(ORDER_METRICS), "dependency_request_duration_seconds_sum", dependency=dependency)

    baseline = median_latency(sample(client, token, 4))
    inject(faults, "network_latency", "order", delay_ms=100, peer="postgres")
    postgres, payment = dependency_seconds("postgres"), dependency_seconds("payment")

    during = sample(client, token, 5)
    assert all(status == 201 for status, _ in during), during
    assert median_latency(during) >= baseline + 0.2, (baseline, during)
    # insert + update per order, each at least one delayed round trip; the order -> payment edge is untouched
    assert dependency_seconds("postgres") - postgres >= 0.9
    assert dependency_seconds("payment") - payment < 0.25


def test_network_fault_reapplied_after_container_restart(faults, client, token):
    inject(faults, "network_latency", "payment", delay_ms=300)
    outage = inject(faults, "service_unavailable", "payment")
    faults.delete(f"/faults/{outage['id']}")  # payment starts again in a fresh network namespace
    # a reconcile pass re-installs the qdisc
    assert eventually(lambda: all(elapsed >= 0.3 for _, elapsed in sample(client, token, 3)), timeout=30)


def test_packet_loss_tail_latency(faults, client, token):
    baseline = median_latency(sample(client, token, 6, spacing=0.1))
    inject(faults, "packet_loss", "payment", loss_percent=50)
    during = sample(client, token, 20, spacing=0.1)
    # Retransmissions (tail-loss probes, RTOs) stretch a share of requests; most still succeed.
    slow = [elapsed for _, elapsed in during if elapsed > 2 * baseline + 0.005]
    assert len(slow) >= 4, (baseline, during)
    assert five_xx_ratio(during) < 0.5, during


def test_connection_failure_reject(faults, client, token):
    inject(faults, "connection_failure", "order", peer="payment")
    response, elapsed = place_order(client, token)
    assert response.status_code == 502 and elapsed < 1
    # Unlike service_unavailable, the peer itself is up and healthy: only this path is broken.
    assert httpx.get("http://payment:8000/health").status_code == 200
    # eventually: a previous test (packet loss on payment) may have failed the last scrape
    assert eventually(lambda: prom_value('up{job="payment"}') == 1, timeout=15)


def test_connection_failure_drop(faults, client, token):
    inject(faults, "connection_failure", "order", peer="payment", mode="drop")
    response, elapsed = place_order(client, token)
    # Packets vanish: a timeout, not a refusal. Order's payment call times out (5 s) before the gateway gives up
    # on order (8 s), so order answers 502 and records the failed payment.
    assert response.status_code == 502 and elapsed >= 4.5
    assert response.json()["order"]["status"] == "payment_failed"


# ---------------------------------------------------------------- redeploys (env / entrypoint)


def test_incorrect_endpoint(faults, client, token):
    started = start_time(ORDER_METRICS)
    fault = inject(faults, "incorrect_endpoint", "order", dependency="payment")
    wait_up("http://order:8000/health")
    assert start_time(ORDER_METRICS) > started  # a new process: the deployment is visible

    payments = metric_value(metrics(PAYMENT_METRICS), "payments_total")
    assert status_becomes(client, token, 502)
    assert all(status == 502 for status, _ in sample(client, token, 3, spacing=0.1))
    assert metric_value(metrics(PAYMENT_METRICS), "payments_total") == payments  # payment is never reached
    assert eventually(lambda: loki_streams(
        '{service="order"} |= "dependency_call_failed" |= "Name or service not known"', since="2m"), timeout=30)

    redeployed = start_time(ORDER_METRICS)
    faults.delete(f"/faults/{fault['id']}")  # the baseline is redeployed and healthy when this returns
    assert start_time(ORDER_METRICS) > redeployed
    assert status_becomes(client, token, 201)


def test_incorrect_timeout(faults, client, token):
    inject(faults, "incorrect_timeout", "gateway", timeout_ms=1)
    wait_up("http://gateway:8000/health")
    assert status_becomes(client, token, 504)

    def timeouts():
        return metric_value(metrics(GATEWAY_METRICS), "dependency_requests_total", outcome="timeout")

    before = timeouts()
    response, elapsed = place_order(client, token)
    assert response.status_code == 504 and elapsed < 1  # "timed out" long before any real timeout
    assert timeouts() == before + 1
    for url in ("http://auth:8000/ready", "http://order:8000/ready"):  # the dependencies are fine
        assert httpx.get(url).status_code == 200


def test_bad_configuration_short_token_ttl(faults, client):
    inject(faults, "bad_configuration", "auth", settings={"TOKEN_TTL_SECONDS": 2})
    wait_up("http://auth:8000/health")
    login = eventually(lambda: (r := client.post("/login", json=DEMO_USER)).status_code == 200 and r, timeout=20)
    assert login and login.json()["expires_in"] == 2

    fresh = login.json()["access_token"]
    assert place_order(client, fresh)[0].status_code == 201
    time.sleep(3)
    assert place_order(client, fresh)[0].status_code == 401  # tokens expire almost immediately


def test_bad_deployment_errors(faults, client, token):
    inject(faults, "bad_deployment", "payment", version="1.1.0", error_rate=1.0)
    assert metric_value(metrics(PAYMENT_METRICS), "app_build_info", version="1.1.0") == 1
    assert status_becomes(client, token, 502)
    assert five_xx_ratio(sample(client, token, 4, spacing=0.1)) == 1
    # The release change is in telemetry: a new version series, correlating with the errors.
    assert eventually(lambda: prom_value('app_build_info{job="payment",version="1.1.0"}') == 1, timeout=20)
    assert eventually(lambda: loki_streams('{service="payment"} | json | version="1.1.0"', since="2m"), timeout=30)


def test_bad_deployment_crash_loop(faults, client, token):
    inject(faults, "bad_deployment", "payment", defect="crash")
    assert eventually(lambda: prom_value('up{job="payment"}') == 0, timeout=20)
    assert place_order(client, token)[0].status_code == 502
    # The crash itself is logged (a stopped container, service_unavailable, logs nothing).
    assert eventually(lambda: loki_streams('{service="payment"} |= "Error loading ASGI app"', since="2m"), timeout=30)


# ---------------------------------------------------------------- API rules for M6 types


@pytest.mark.parametrize("body", [
    {"type": "network_latency", "target": "order", "parameters": {"delay_ms": 10, "peer": "order"}},
    {"type": "network_latency", "target": "tempo", "parameters": {"delay_ms": 10}},  # nginx is a target since M8
    {"type": "packet_loss", "target": "payment", "parameters": {"loss_percent": 0}},
    {"type": "connection_failure", "target": "order", "parameters": {}},
    {"type": "connection_failure", "target": "payment", "parameters": {"peer": "order"}},
    {"type": "incorrect_endpoint", "target": "order", "parameters": {"dependency": "redis"}},
    {"type": "bad_configuration", "target": "auth", "parameters": {"settings": {"DEMO_USERS": "x:y"}}},
    {"type": "bad_configuration", "target": "order", "parameters": {"settings": {}}},
    {"type": "bad_deployment", "target": "gateway"},
    {"type": "bad_deployment", "target": "payment", "parameters": {"defect": "typo"}},
])
def test_invalid_infra_faults_rejected(faults, body):
    assert faults.post("/faults", json=body).status_code == 422


def test_network_conflict(faults):
    inject(faults, "network_latency", "payment", delay_ms=10)
    clash = faults.post("/faults", json={"type": "packet_loss", "target": "payment"})
    assert clash.status_code == 409  # one netem/iptables set per network namespace


def test_redeploy_conflicts_with_container_state(faults):
    inject(faults, "bad_configuration", "order", settings={"DB_POOL_MAX_SIZE": 2})
    clash = faults.post("/faults", json={"type": "dependency_timeout", "target": "order"})
    assert clash.status_code == 409


def test_no_mechanism_names_in_logs(faults, client, token):
    inject(faults, "network_latency", "order", delay_ms=5, peer="payment")
    inject(faults, "bad_configuration", "auth", settings={"TOKEN_TTL_SECONDS": 600})
    wait_up("http://auth:8000/health")
    sample(client, token, 3)
    loki_caught_up(client, token)
    # The test runner's own output (e.g. a failed assertion quoting a fault id) is not the diagnostic plane.
    for needle in ("netem", "iptables", "io.testbed", "flt-"):
        assert loki_streams(f'{{service=~".+", service!="tests"}} |= "{needle}"', since="1h") == [], needle
