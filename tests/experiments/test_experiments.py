"""M7: reproducible experiments with hidden ground truth (experiment-runner, control plane).

Runs use shortened phases via `overrides`; the scenario files keep the full durations.
"""

import json

import httpx
import pytest

from conftest import EXPERIMENT_RUNNER_URL, PROMETHEUS_URL, eventually, inject, loki_streams

PHASES = ["baseline", "inject", "observe", "record", "remove", "recovery"]
SHORT = {"durations": {"baseline_s": 8, "observe_s": 8, "recovery_window_s": 4, "recovery_timeout_s": 30}}


@pytest.fixture(scope="module")
def runner():
    with httpx.Client(base_url=EXPERIMENT_RUNNER_URL, timeout=300) as client:
        yield client


@pytest.fixture(autouse=True)
def clean(runner, faults):
    yield
    for experiment in runner.get("/experiments", params={"state": "running"}).json():
        runner.delete(f"/experiments/{experiment['id']}")
    faults.delete("/faults")


def run(runner, scenario: str, overrides: dict = SHORT) -> dict:
    response = runner.post("/experiments", params={"wait": "true"},
                           json={"scenario": scenario, "overrides": overrides})
    assert response.status_code == 202, response.text
    return response.json()


def test_scenarios_are_valid(runner):
    scenarios = runner.get("/scenarios").json()
    assert len(scenarios) >= 8
    assert [s["name"] for s in scenarios if not s["valid"]] == []


def test_experiment_lifecycle(runner, faults):
    experiment = run(runner, "payment-latency")
    assert experiment["state"] == "completed", experiment["error"]
    assert experiment["id"].startswith("exp-")

    phases = experiment["phases"]
    assert [p["name"] for p in phases] == PHASES
    assert all(p["started_at"] <= p["ended_at"] for p in phases)
    assert all(a["ended_at"] <= b["started_at"] for a, b in zip(phases, phases[1:]))

    (truth,) = experiment["ground_truth"]["faults"]
    assert truth["type"] == "payment_latency" and truth["parameters"]["latency_ms"] == 1500
    stored = faults.get(f"/faults/{truth['id']}").json()
    assert stored["experiment_id"] == experiment["id"] and stored["state"] == "removed"
    assert faults.get("/faults", params={"state": "active"}).json() == []

    # The investigator's view carries the window and symptoms, never the cause.
    incident = json.dumps(experiment["incident"])
    assert experiment["incident"]["symptoms"] == ["slow"]
    assert "payment" not in incident and truth["id"] not in incident

    verdict = experiment["verdict"]
    assert verdict["expected_symptom_seen"] and verdict["recovered"]
    results = experiment["results"]
    assert results["observe"]["p95_s"] > results["baseline"]["p95_s"] + 1
    assert experiment["telemetry"]["observe"]["gateway_p95_s"] > 1  # Prometheus saw it too


def test_error_experiment(runner):
    experiment = run(runner, "payment-error")
    assert experiment["state"] == "completed", experiment["error"]
    assert "errors" in experiment["verdict"]["observed"]
    assert experiment["verdict"]["recovered"]


def test_one_experiment_at_a_time_and_abort(runner, faults):
    started = runner.post("/experiments", json={"scenario": "payment-latency", "overrides": {
        "durations": {"baseline_s": 3, "observe_s": 120}}})
    assert started.status_code == 202
    experiment_id = started.json()["id"]
    clash = runner.post("/experiments", json={"scenario": "payment-error"})
    assert clash.status_code == 409

    assert eventually(lambda: runner.get(f"/experiments/{experiment_id}").json()["phase"] == "observe", timeout=20)
    assert len(faults.get("/faults", params={"state": "active"}).json()) == 1
    aborted = runner.delete(f"/experiments/{experiment_id}").json()
    assert aborted["state"] == "aborted"
    assert faults.get("/faults", params={"state": "active"}).json() == []


def test_abort_during_injection_leaves_no_fault(runner, faults):
    """M10: an abort while the injector is still redeploying used to lose the fault (it stayed active)."""
    started = runner.post("/experiments", json={"scenario": "bad-deployment", "overrides": {
        "durations": {"baseline_s": 3, "observe_s": 120}}})
    assert started.status_code == 202
    experiment_id = started.json()["id"]
    assert eventually(lambda: runner.get(f"/experiments/{experiment_id}").json()["phase"] == "inject",
                      timeout=20, interval=0.1)

    aborted = runner.delete(f"/experiments/{experiment_id}").json()
    assert aborted["state"] == "aborted", aborted
    assert [f["type"] for f in aborted["ground_truth"]["faults"]] == ["bad_deployment"]
    assert faults.get("/faults", params={"state": "active"}).json() == []


def test_refuses_to_start_with_active_faults(runner, faults):
    inject(faults, "payment_latency", latency_ms=10)
    assert runner.post("/experiments", json={"scenario": "payment-error"}).status_code == 409


@pytest.mark.parametrize("body", [
    {"scenario": "no-such-scenario"},
    {"scenario": "payment-latency", "overrides": {"durations": {"baseline_s": 0}}},
    {"scenario": "payment-latency", "overrides": {"traffic": {"rate": 1000}}},
])
def test_invalid_requests(runner, body):
    assert runner.post("/experiments", json=body).status_code == 422


def test_runner_invisible_to_diagnostic_plane(runner):
    if not runner.get("/experiments").json():  # needs a recorded experiment (also when run alone)
        assert run(runner, "payment-error")["state"] == "completed"
    assert loki_streams('{service="experiment-runner"}', since="1h") == []
    assert loki_streams('{service=~".+", service!="tests"} |= "exp-"', since="1h") == []
    jobs = {t["labels"]["job"] for t in httpx.get(f"{PROMETHEUS_URL}/api/v1/targets").json()["data"]["activeTargets"]}
    assert not any("experiment" in job for job in jobs)
