import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.experiments import Injector, Runner
from app.scenarios import Overrides, apply_overrides, load_all
from app.store import ExperimentStore, now


class _JsonFormatter(logging.Formatter):
    """Plain JSON to stdout. Control plane: Alloy drops these logs, so they never reach Loki."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": now(), "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in record.__dict__.items() if k in ("experiment_id", "scenario", "error")})
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("experiment-runner")

store = ExperimentStore(settings.database_path)
lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    injector = Injector(settings.fault_injector_url)
    app.state.runner = Runner(store, injector, settings.base_url, settings.prometheus_url)
    try:
        await app.state.runner.abort_leftovers()
    except Exception as exc:  # the injector may still be starting; `make recover` also cleans up
        logger.warning("abort_leftovers_failed", extra={"error": repr(exc)})
    yield
    await app.state.runner.aclose()
    await injector.aclose()


app = FastAPI(title="experiment-runner", lifespan=lifespan)


class ExperimentRequest(BaseModel):
    scenario: str
    overrides: Overrides | None = None


def summary(experiment: dict) -> str:
    """Human-readable result for `make experiment`."""
    lines = [f"{experiment['id']}  scenario={experiment['scenario']}  state={experiment['state']}"]
    if experiment.get("error"):
        lines.append(f"error: {experiment['error']}")
    for fault in experiment["ground_truth"]["faults"]:
        lines.append(f"ground truth: {fault['type']} on {fault['target']} {json.dumps(fault['parameters'])}")
    results = experiment["results"]
    for phase in ("baseline", "observe"):
        if phase in results:
            r = results[phase]
            lines.append(f"{phase:9s} p50={r['p50_s']:.3f}s p95={r['p95_s']:.3f}s errors={r['error_ratio']:.0%}"
                         f" 401={r['auth_error_ratio']:.0%} rps={r['rps']}"
                         + (f" skipped={r['skipped']}" if r.get("skipped") else ""))
    status = (experiment.get("domains") or {}).get("status")
    if status:
        lines.append("domains:  " + "  ".join(
            f"{domain}={view['status']}" + (f"({','.join(view['signals'])})" if view["signals"] else "")
            for domain, view in status.items()))
    if experiment.get("incident"):
        lines.append(f"incident: onset={experiment['incident']['start']} symptoms={experiment['incident']['symptoms']}")
    verdict = experiment.get("verdict")
    if verdict:
        lines.append(f"verdict: expected={verdict['expected']} observed={verdict['observed'] or ['none']}"
                     f" expected_seen={verdict['expected_symptom_seen']} recovered={verdict['recovered']}"
                     f" recovery_seconds={verdict['recovery_seconds']} evidence_matched={verdict.get('evidence_matched')}")
    return "\n".join(lines) + "\n"


def render(experiment: dict, format: str):
    return PlainTextResponse(summary(experiment)) if format == "text" else experiment


@app.get("/scenarios")
async def list_scenarios(format: Literal["json", "text"] = "json"):
    """Every scenario file, with its fault specs checked by the injector."""
    scenarios, errors = load_all(settings.scenarios_dir)
    listed = [{"name": name, "valid": False, "errors": [error]} for name, error in errors.items()]
    for scenario in scenarios.values():
        problems = []
        for spec in scenario.faults:
            ok, detail = await app.state.runner.injector.validate(spec.request())
            if not ok:
                problems.append({"fault": spec.type, "detail": detail})
        listed.append({**scenario.model_dump(), "valid": not problems, "errors": problems})
    listed.sort(key=lambda s: s["name"])
    if format == "text":
        return PlainTextResponse("".join(
            f"{s['name']:22} {'ok ' if s['valid'] else 'BAD'} {s.get('expect', ''):12} "
            f"{s.get('description', '')}{' ' + json.dumps(s['errors']) if s['errors'] else ''}\n" for s in listed))
    return listed


@app.post("/experiments", status_code=202)
async def create_experiment(request: ExperimentRequest, wait: bool = False,
                            format: Literal["json", "text"] = "json"):
    scenarios, errors = load_all(settings.scenarios_dir)
    scenario = scenarios.get(request.scenario)
    if scenario is None:
        detail = errors.get(f"{request.scenario}.json") or f"unknown scenario; known: {sorted(scenarios)}"
        raise HTTPException(422, detail)
    try:
        scenario = apply_overrides(scenario, request.overrides)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json(include_url=False)))
    runner = app.state.runner
    async with lock:
        if runner.busy:
            raise HTTPException(409, "another experiment is running")
        active = await runner.injector.active_faults()
        if active:  # a polluted baseline would make the experiment meaningless
            raise HTTPException(409, f"{len(active)} fault(s) already active; run `make recover` first")
        for spec in scenario.faults:
            ok, detail = await runner.injector.validate(spec.request())
            if not ok:
                raise HTTPException(422, {"fault": spec.type, "detail": detail})
        experiment = runner.start(scenario)
    logger.info("experiment_started", extra={"experiment_id": experiment["id"], "scenario": scenario.name})
    if wait:
        await asyncio.shield(runner.task)  # a dropped client connection doesn't cancel the experiment
        experiment = store.get(experiment["id"])
    return render(experiment, format)


@app.get("/experiments")
async def list_experiments(state: Literal["running", "completed", "failed", "aborted"] | None = None) -> list[dict]:
    return store.list(state)


@app.get("/experiments/{experiment_id}")
async def get_experiment(experiment_id: str, format: Literal["json", "text"] = "json"):
    experiment = store.get(experiment_id)
    if experiment is None:
        raise HTTPException(404, "experiment not found")
    return render(experiment, format)


@app.delete("/experiments/{experiment_id}")
async def abort_experiment(experiment_id: str) -> dict:
    """Aborts the running experiment: traffic stops and its faults are removed."""
    experiment = store.get(experiment_id)
    if experiment is None:
        raise HTTPException(404, "experiment not found")
    runner = app.state.runner
    if experiment["state"] == "running" and runner.busy:
        runner.task.cancel()
        await asyncio.gather(runner.task, return_exceptions=True)
    return store.get(experiment_id)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "experiment-runner"}
