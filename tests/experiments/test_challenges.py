"""M8: diagnostic challenge scenarios, verified end to end through the experiment-runner.

- same-symptom: eight causes, one external symptom (POST /orders p95 > 2 s, still 201 at 10 rps); each
  scenario's per-domain view must match the evidence it declares.
- model-breaking: every dashboard domain NORMAL while clients are slow; the delay is visible only in nginx.
- misleading-correlation: a harmless deployment precedes the real cause; rolling it back doesn't help.
"""

import time
from datetime import datetime

import httpx
import pytest

from conftest import EXPERIMENT_RUNNER_URL, eventually, loki_streams, place_order

SAME_SYMPTOM = ["slow-payment", "slow-db-query", "slow-pool-exhaustion", "slow-redis", "slow-cpu", "slow-network",
                "slow-proxy-network", "slow-proxy-config"]
SHORT = {"durations": {"baseline_s": 10, "observe_s": 25, "recovery_window_s": 5, "recovery_timeout_s": 60}}


@pytest.fixture(scope="module")
def runner():
    with httpx.Client(base_url=EXPERIMENT_RUNNER_URL, timeout=600) as client:
        yield client


@pytest.fixture(autouse=True)
def clean(runner, faults):
    yield
    for experiment in runner.get("/experiments", params={"state": "running"}).json():
        runner.delete(f"/experiments/{experiment['id']}")
    faults.delete("/faults")


def ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def test_challenge_catalog(runner):
    scenarios = {s["name"]: s for s in runner.get("/scenarios").json()}
    assert all(scenarios[name]["valid"] for name in SAME_SYMPTOM + ["deploy-then-payment"])
    assert sum("model-breaking" in s["tags"] for s in scenarios.values()) >= 2
    assert sum("misleading-correlation" in s["tags"] for s in scenarios.values()) >= 1
    assert {s["traffic"]["rate"] for s in scenarios.values() if "same-symptom" in s["tags"]} == {10}


@pytest.mark.parametrize("scenario", SAME_SYMPTOM)
def test_same_symptom(runner, scenario):
    response = runner.post("/experiments", params={"wait": "true"}, json={"scenario": scenario, "overrides": SHORT})
    assert response.status_code == 202, response.text
    experiment = response.json()
    assert experiment["state"] == "completed", experiment["error"]
    observed = experiment["results"]["observe"]
    assert observed["p95_s"] > 2, observed  # the shared symptom...
    # ...with successful responses. Not 0: redeploy-based causes (the pool size) restart order, and the
    # few 502s of that restart weigh more in the shortened test phases (3% at full length, up to ~7% here).
    assert observed["error_ratio"] < 0.1, observed
    status = experiment["domains"]["status"]
    assert experiment["verdict"]["evidence_matched"], status  # ...but each cause leaves its own evidence
    assert experiment["verdict"]["recovered"]

    spec = runner.get("/scenarios").json()
    if "model-breaking" in next(s for s in spec if s["name"] == scenario)["tags"]:
        assert all(view["status"] == "NORMAL" for view in status.values()), status
        # Only the proxy's own logs show where the time went.
        slow = loki_streams('{service="nginx"} | json | request_time_s > 2', since="3m")
        assert slow, "no slow requests in nginx logs"


def test_misleading_correlation_rollback_does_not_help(runner, faults, client, token):
    started = runner.post("/experiments", json={"scenario": "deploy-then-payment", "overrides": {
        "time_scale": 0.1, "durations": {"baseline_s": 10, "observe_s": 50, "recovery_window_s": 5}}})
    assert started.status_code == 202, started.text
    experiment_id = started.json()["id"]

    def truth():
        return runner.get(f"/experiments/{experiment_id}").json()["ground_truth"]["faults"]

    assert eventually(lambda: len(truth()) == 2, timeout=80), "the real cause was never injected"
    deploy, cause = truth()
    assert deploy["type"] == "bad_deployment" and cause["type"] == "payment_latency"
    time.sleep(3)

    faults.delete(f"/faults/{deploy['id']}")  # the rollback: order is back on its baseline release
    assert eventually(lambda: place_order(client, token)[0].status_code == 201, timeout=30)
    still_slow = [place_order(client, token)[1] for _ in range(3)]
    assert min(still_slow) > 2, still_slow  # technically successful, the incident persists

    experiment = eventually(lambda: (e := runner.get(f"/experiments/{experiment_id}").json())["state"] != "running"
                            and e, timeout=180)
    assert experiment and experiment["state"] == "completed", experiment
    onset = ts(experiment["incident"]["start"])
    assert ts(deploy["created_at"]) < onset - 20  # the deployment happened well before the incident
    assert abs(onset - ts(cause["created_at"])) < 10  # the incident starts with the real cause
    assert experiment["verdict"]["evidence_matched"], experiment["domains"]["status"]
    assert experiment["verdict"]["recovered"]
