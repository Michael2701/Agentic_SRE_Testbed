"""Experiment lifecycle: baseline -> inject -> observe -> record -> remove -> recovery (project.md, M7).

Traffic runs from the start of the baseline until recovery is verified. The record separates what only the
control plane may know (`ground_truth`: the injected faults) from what an investigator would be told
(`incident`: time window and observed symptoms, no cause).
"""

import asyncio
import logging
import time
from datetime import datetime

import httpx

from app.scenarios import Scenario
from app.store import ExperimentStore, iso
from app.traffic import Traffic, summarize

logger = logging.getLogger("experiments")

PHASES = ("baseline", "inject", "observe", "record", "remove", "recovery")
ORDERS = 'job="gateway",route="/orders",method="POST"'


class ExperimentError(Exception):
    pass


def symptoms(baseline: dict, window: dict) -> list[str]:
    """External symptoms of `window` relative to the baseline (client-side view of POST/GET /orders)."""
    found = []
    if window["p95_s"] >= max(2 * baseline["p95_s"], baseline["p95_s"] + 0.05):
        found.append("slow")
    if window["error_ratio"] >= baseline["error_ratio"] + 0.05:
        found.append("errors")
    if window["auth_error_ratio"] >= baseline["auth_error_ratio"] + 0.05:
        found.append("auth_errors")
    return found


class Injector:
    """Thin client of the fault-injector API (the only way the runner touches the system)."""

    def __init__(self, url: str):
        self._http = httpx.AsyncClient(base_url=url, timeout=120)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def active_faults(self) -> list[dict]:
        response = await self._http.get("/faults", params={"state": "active"})
        response.raise_for_status()
        return response.json()

    async def validate(self, spec: dict) -> tuple[bool, object]:
        response = await self._http.post("/faults/validate", json=spec)
        return response.status_code == 200, response.json()

    async def inject(self, spec: dict, experiment_id: str) -> dict:
        response = await self._http.post("/faults", json={**spec, "experiment_id": experiment_id})
        if response.status_code != 201:
            raise ExperimentError(f"inject {spec['type']}: HTTP {response.status_code} {response.text}")
        return response.json()

    async def remove(self, fault_id: str) -> None:
        response = await self._http.delete(f"/faults/{fault_id}")
        if response.status_code not in (200, 404):
            raise ExperimentError(f"remove {fault_id}: HTTP {response.status_code} {response.text}")


class Runner:
    def __init__(self, store: ExperimentStore, injector: Injector, base_url: str, prometheus_url: str):
        self.store = store
        self.injector = injector
        self.base_url = base_url
        self._prometheus = httpx.AsyncClient(base_url=prometheus_url, timeout=10)
        self.task: asyncio.Task | None = None

    async def aclose(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._prometheus.aclose()

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, scenario: Scenario) -> dict:
        experiment = self.store.create({
            "scenario": scenario.name, "spec": scenario.model_dump(), "phase": None, "error": None,
            "ground_truth": {"faults": []}, "incident": None, "phases": [], "results": {}, "telemetry": {},
            "verdict": None,
        })
        self.task = asyncio.create_task(self._run(experiment, scenario))
        return experiment

    async def abort_leftovers(self) -> None:
        """After a runner restart: experiments still `running` lost their task; remove their faults."""
        for experiment in self.store.list("running"):
            await self._remove_faults(experiment)
            experiment.update(state="aborted", error="runner restarted during the experiment")
            self.store.save(experiment)
            logger.warning("experiment_aborted", extra={"experiment_id": experiment["id"]})

    # ---------------------------------------------------------------- lifecycle

    def _enter(self, experiment: dict, phase: str) -> float:
        started = time.time()
        if experiment["phases"]:
            experiment["phases"][-1]["ended_at"] = iso(started)
        experiment["phase"] = phase
        experiment["phases"].append({"name": phase, "started_at": iso(started), "ended_at": None})
        self.store.save(experiment)
        return started

    async def _run(self, experiment: dict, scenario: Scenario) -> None:
        durations = scenario.durations
        traffic = Traffic(self.base_url, scenario.traffic.rate)
        try:
            baseline_start = self._enter(experiment, "baseline")
            async with httpx.AsyncClient(base_url=self.base_url, timeout=5) as http:
                if (await http.get("/ready")).status_code != 200:
                    raise ExperimentError("system is not ready; baseline can't be established")
            await traffic.start()
            await asyncio.sleep(durations.baseline_s)

            inject_start = self._enter(experiment, "inject")
            for spec in scenario.faults:
                fault = await self.injector.inject(spec.model_dump(exclude_none=True), experiment["id"])
                experiment["ground_truth"]["faults"].append(
                    {key: fault[key] for key in ("id", "type", "target", "parameters", "created_at")})
                self.store.save(experiment)

            observe_start = self._enter(experiment, "observe")
            await asyncio.sleep(durations.observe_s)

            observe_end = self._enter(experiment, "record")
            await traffic.settle(observe_end)
            baseline = summarize(traffic.window(baseline_start, inject_start))
            observed = summarize(traffic.window(observe_start, observe_end))
            found = symptoms(baseline, observed)
            experiment["results"] = {"baseline": baseline, "observe": observed}
            experiment["incident"] = {"start": iso(inject_start), "end": None, "entry_point": "nginx /orders",
                                      "symptoms": found}

            self._enter(experiment, "remove")
            await self._remove_faults(experiment)

            removed = self._enter(experiment, "recovery")
            experiment["incident"]["end"] = iso(removed)
            recovery = await self._verify_recovery(traffic, baseline, removed, durations)
            experiment["results"]["recovery"] = recovery

            experiment["phases"][-1]["ended_at"] = iso(time.time())
            experiment["telemetry"] = await self._telemetry(experiment["phases"])
            experiment["verdict"] = {
                "expected": scenario.expect,
                "observed": found,
                "degraded": bool(found),
                "expected_symptom_seen": (not found) if scenario.expect == "none" else scenario.expect in found,
                "recovered": recovery["recovered"],
                "recovery_seconds": recovery["recovery_seconds"],
            }
            experiment.update(state="completed", phase=None)
        except asyncio.CancelledError:
            await self._remove_faults(experiment)
            experiment.update(state="aborted", error="aborted")
        except Exception as exc:
            logger.error("experiment_failed", extra={"experiment_id": experiment["id"], "error": repr(exc)})
            await self._remove_faults(experiment)
            experiment.update(state="failed", error=repr(exc))
        finally:
            await traffic.stop()
            self.store.save(experiment)

    async def _remove_faults(self, experiment: dict) -> None:
        for fault in experiment["ground_truth"]["faults"]:
            try:
                await self.injector.remove(fault["id"])
            except Exception as exc:  # the next attempt (or `make recover`) cleans up
                logger.error("fault_remove_failed", extra={"experiment_id": experiment["id"], "error": repr(exc)})

    async def _verify_recovery(self, traffic: Traffic, baseline: dict, removed: float, durations) -> dict:
        """Consecutive windows after removal; the first one without symptoms proves recovery."""
        windows = []
        start = removed
        while start - removed < durations.recovery_timeout_s:
            end = start + durations.recovery_window_s
            await asyncio.sleep(max(0.0, end - time.time()))
            await traffic.settle(end)
            stats = summarize(traffic.window(start, end))
            found = symptoms(baseline, stats)
            enough = stats["requests"] >= 0.5 * traffic.rate * durations.recovery_window_s
            windows.append({"start": iso(start), "symptoms": found, "p95_s": stats["p95_s"],
                            "error_ratio": stats["error_ratio"], "requests": stats["requests"]})
            if enough and not found:
                return {"recovered": True, "recovery_seconds": round(start - removed, 1), "windows": windows,
                        "healthy_window": stats}
            start = end
        return {"recovered": False, "recovery_seconds": None, "windows": windows, "healthy_window": None}

    async def _telemetry(self, phases: list[dict]) -> dict:
        """What the diagnostic plane recorded per phase (gateway POST /orders), for comparison."""
        snapshot = {}
        for phase in phases:
            if phase["name"] not in ("baseline", "observe"):
                continue
            start = datetime.fromisoformat(phase["started_at"]).timestamp()
            end = datetime.fromisoformat(phase["ended_at"]).timestamp()
            rng = f"{max(15, round(end - start))}s"
            snapshot[phase["name"]] = {
                "gateway_p95_s": await self._query(
                    f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{ORDERS}}}[{rng}])))",
                    end),
                "gateway_5xx_ratio": await self._query(
                    f'(sum(rate(http_requests_total{{{ORDERS},status=~"5.."}}[{rng}])) or vector(0))'
                    f" / sum(rate(http_requests_total{{{ORDERS}}}[{rng}]))", end),
            }
        return snapshot

    async def _query(self, expr: str, at: float) -> float | None:
        try:
            response = await self._prometheus.get("/api/v1/query", params={"query": expr, "time": at})
            result = response.json()["data"]["result"]
            value = float(result[0]["value"][1]) if result else None
            return None if value is None or value != value else round(value, 4)  # NaN -> None
        except (httpx.HTTPError, KeyError, ValueError):
            return None
