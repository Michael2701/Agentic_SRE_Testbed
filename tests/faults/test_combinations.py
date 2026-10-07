"""M10: faults that are allowed together must be enforceable and removable together.

Each case once broke the injector: one fault that could not be enforced stopped the whole reconcile pass
(and failed every new fault), and `DELETE /faults` gave up at the first revert that failed.
"""

import time

import httpx
import pytest

from conftest import eventually, inject, median_latency, sample

pytestmark = pytest.mark.usefixtures("recover_after")


def enforce_errors(faults) -> dict[str, str]:
    return {f["id"]: f["error"] for f in faults.get("/faults", params={"state": "active"}).json() if f["error"]}


def test_hogs_in_a_paused_container(faults):
    """No exec into a paused container: the hogs wait, unrelated faults still apply, and removal succeeds."""
    inject(faults, "cpu_saturation", "payment", workers=2)
    inject(faults, "dependency_timeout", "payment")
    inject(faults, "db_slow_query", delay_ms=50)  # unrelated: used to fail because of the two above

    time.sleep(5)  # two reconcile passes
    assert enforce_errors(faults) == {}

    response = faults.delete("/faults")  # the hogs can only be stopped once the pause is gone
    assert response.status_code == 200, response.text
    assert all(f["state"] == "removed" for f in response.json())


def test_redeploy_keeps_faultpoint_config(faults):
    """A redeploy of a service with a faultpoint fault waits for it, so the config can be pushed again."""
    inject(faults, "intermittent_errors", "order", error_rate=0.2)
    inject(faults, "bad_configuration", "order", settings={"DB_POOL_MAX_SIZE": 12})

    def pushed():
        try:
            return httpx.get("http://order:8000/__faults", timeout=2).json().get("error_probability") == 0.2
        except httpx.HTTPError:
            return False

    assert eventually(pushed, timeout=30)
    assert enforce_errors(faults) == {}


def test_network_fault_removed_while_paused(faults, client, token):
    """A paused container keeps its network namespace: the rules must be cleared even then."""
    baseline = median_latency(sample(client, token, 4))
    shaped = inject(faults, "network_latency", "payment", delay_ms=1500)
    paused = inject(faults, "dependency_timeout", "payment")

    assert faults.delete(f"/faults/{shaped['id']}").json()["state"] == "removed"
    assert faults.delete(f"/faults/{paused['id']}").json()["state"] == "removed"

    assert median_latency(sample(client, token, 4)) < baseline + 0.5
