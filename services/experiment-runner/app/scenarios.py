"""Scenario files (`experiments/scenarios/<name>.json`, mounted read-only at /scenarios).

A scenario is the recipe of an experiment: which faults to inject, how much traffic, how long each phase
lasts and which external symptom it is expected to produce. Fault specs are validated by the injector
(`POST /faults/validate`), which owns the fault catalog.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FaultSpec(_Model):
    type: str
    target: str | None = None
    parameters: dict = Field(default_factory=dict)
    at_s: float = Field(default=0, ge=0, le=3600)  # offset from the start of the inject phase (M8 timelines)
    # Parameters computed at inject time from what the target used during the baseline, so the fault has the
    # same effect on a faster or slower host (M9). Only {"cpus": f}: f x the CPU the target needs for the
    # scenario's request rate (CPU per request in the baseline x rate).
    baseline_relative: dict[Literal["cpus"], float] | None = None

    @model_validator(mode="after")
    def _relative_fits(self):
        if self.baseline_relative and (self.target is None or any(f <= 0 for f in self.baseline_relative.values())):
            raise ValueError("baseline_relative needs an explicit target and positive factors")
        return self

    def request(self, parameters: dict | None = None) -> dict:
        """The body for the injector (offset and calibration are the runner's business)."""
        body = self.model_dump(exclude_none=True, exclude={"at_s", "baseline_relative"})
        if parameters:
            body["parameters"] = {**body["parameters"], **parameters}
        return body


class Traffic(_Model):
    rate: float = Field(default=5, gt=0, le=50)  # requests per second
    # Like a client with a fixed connection pool: at this many requests in flight a tick is skipped. Without
    # it (open loop) a saturated service either copes or its queue grows until timeouts (M8, Q4).
    max_in_flight: int | None = Field(default=None, ge=1, le=1000)


class Durations(_Model):
    baseline_s: float = Field(default=30, ge=3, le=600)
    observe_s: float = Field(default=30, ge=3, le=600)
    recovery_window_s: float = Field(default=10, ge=2, le=120)  # a healthy window this long = recovered
    recovery_timeout_s: float = Field(default=90, ge=5, le=900)


Symptom = Literal["slow", "errors", "auth_errors"]
Domain = Literal["application", "database", "redis", "payment"]
Tag = Literal["basic", "same-symptom", "misleading-correlation", "model-breaking"]


class Evidence(_Model):
    """Expected per-domain view during the incident (ground truth side; checked by tests)."""

    degraded_domains: list[Domain] = Field(default_factory=list)
    normal_domains: list[Domain] = Field(default_factory=list)


class Scenario(_Model):
    name: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    description: str = ""
    faults: list[FaultSpec] = Field(min_length=1)
    traffic: Traffic = Traffic()
    durations: Durations = Durations()
    expect: Symptom | Literal["none"]
    tags: list[Tag] = Field(default_factory=lambda: ["basic"])
    evidence: Evidence = Evidence()

    @model_validator(mode="after")
    def _timeline_fits(self):
        latest = max(fault.at_s for fault in self.faults)
        if latest and self.durations.observe_s < latest + 10:
            raise ValueError("observe_s must exceed the latest fault offset (at_s) by at least 10s")
        return self


class Overrides(_Model):
    """Per-run changes merged over the scenario (e.g. shorter phases in tests); the file stays the recipe."""

    traffic: dict | None = None
    durations: dict | None = None
    time_scale: float | None = Field(default=None, gt=0, le=1)  # scales fault offsets (at_s), e.g. 0.1 in tests


def load_all(directory: str) -> tuple[dict[str, Scenario], dict[str, str]]:
    """Returns (valid scenarios by name, errors by file name)."""
    scenarios, errors = {}, {}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            scenario = Scenario(**json.loads(path.read_text()))
        except (ValueError, ValidationError) as exc:
            errors[path.name] = str(exc)
            continue
        if scenario.name != path.stem:
            errors[path.name] = f"name {scenario.name!r} must match the file name"
            continue
        scenarios[scenario.name] = scenario
    return scenarios, errors


def apply_overrides(scenario: Scenario, overrides: Overrides | None) -> Scenario:
    """Raises ValidationError when a merged value is invalid."""
    if overrides is None:
        return scenario
    data = scenario.model_dump()
    if overrides.time_scale:
        for fault in data["faults"]:
            fault["at_s"] = round(fault["at_s"] * overrides.time_scale, 1)
    return Scenario(**{
        **data,
        "traffic": {**scenario.traffic.model_dump(), **(overrides.traffic or {})},
        "durations": {**scenario.durations.model_dump(), **(overrides.durations or {})},
    })
