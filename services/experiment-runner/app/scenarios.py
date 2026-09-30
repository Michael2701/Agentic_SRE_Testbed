"""Scenario files (`experiments/scenarios/<name>.json`, mounted read-only at /scenarios).

A scenario is the recipe of an experiment: which faults to inject, how much traffic, how long each phase
lasts and which external symptom it is expected to produce. Fault specs are validated by the injector
(`POST /faults/validate`), which owns the fault catalog.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FaultSpec(_Model):
    type: str
    target: str | None = None
    parameters: dict = Field(default_factory=dict)


class Traffic(_Model):
    rate: float = Field(default=5, gt=0, le=50)  # requests per second


class Durations(_Model):
    baseline_s: float = Field(default=30, ge=3, le=600)
    observe_s: float = Field(default=30, ge=3, le=600)
    recovery_window_s: float = Field(default=10, ge=2, le=120)  # a healthy window this long = recovered
    recovery_timeout_s: float = Field(default=90, ge=5, le=900)


Symptom = Literal["slow", "errors", "auth_errors"]


class Scenario(_Model):
    name: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    description: str = ""
    faults: list[FaultSpec] = Field(min_length=1)
    traffic: Traffic = Traffic()
    durations: Durations = Durations()
    expect: Symptom | Literal["none"]


class Overrides(_Model):
    """Per-run changes merged over the scenario (e.g. shorter phases in tests); the file stays the recipe."""

    traffic: dict | None = None
    durations: dict | None = None


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
    return Scenario(**{
        **scenario.model_dump(),
        "traffic": {**scenario.traffic.model_dump(), **(overrides.traffic or {})},
        "durations": {**scenario.durations.model_dump(), **(overrides.durations or {})},
    })
