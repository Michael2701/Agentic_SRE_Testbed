"""Fault types: allowed targets, parameter schema and the mechanism that realizes them."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

APP_SERVICES = frozenset({"gateway", "auth", "order", "payment"})
FAULTPOINT_SERVICES = frozenset({"auth", "order", "payment"})  # services with the libs/faultpoint hook


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(_Params):
    pass


class PaymentLatencyParams(_Params):
    latency_ms: int = Field(ge=1, le=30_000)
    jitter_ms: int = Field(default=0, ge=0, le=30_000)
    probability: float = Field(default=1.0, gt=0, le=1)


class PaymentErrorParams(_Params):
    status_code: int = Field(default=500, ge=500, le=599)
    probability: float = Field(default=1.0, gt=0, le=1)


class IntermittentErrorsParams(_Params):
    error_rate: float = Field(default=0.3, gt=0, le=1)
    status_code: int = Field(default=500, ge=500, le=599)


class CpuSaturationParams(_Params):
    workers: int = Field(default=2, ge=1, le=8)


class MemoryPressureParams(_Params):
    mb: int = Field(default=200, ge=16, le=2048)


class DbSlowQueryParams(_Params):
    delay_ms: int = Field(ge=1, le=10_000)
    operations: list[Literal["insert", "update"]] = Field(default=["insert", "update"], min_length=1)


class _Periodic(_Params):
    """A resource is held for `<hold>` ms out of every `interval_ms`."""

    @model_validator(mode="after")
    def _interval_exceeds_hold(self):
        hold = getattr(self, "hold_ms", None) or getattr(self, "pause_ms")
        if self.interval_ms <= hold:
            raise ValueError("interval_ms must be greater than the hold/pause duration")
        return self


class DbLockContentionParams(_Periodic):
    hold_ms: int = Field(default=1500, ge=50, le=10_000)
    interval_ms: int = Field(default=2000, ge=100, le=60_000)


class RedisLatencyParams(_Periodic):
    pause_ms: int = Field(ge=10, le=5_000)
    interval_ms: int = Field(default=1000, ge=20, le=60_000)


@dataclass(frozen=True)
class FaultType:
    params: type[_Params]
    targets: frozenset[str]
    # faultpoint      config pushed to the target's `PUT /__faults` (libs/faultpoint)
    # container_stop  docker stop / start           container_pause  docker pause / unpause
    # exec_cpu        CPU burner processes inside the target container (docker exec)
    # exec_memory     memory hog process inside the target container (docker exec)
    # pg_trigger      pg_sleep trigger on `orders`  pg_exhaustion    hold all normal connection slots
    # pg_lock         periodic LOCK TABLE orders     redis_pause      periodic CLIENT PAUSE
    mechanism: str
    default_target: str | None = None


def _single(name: str) -> frozenset[str]:
    return frozenset({name})


FAULT_TYPES: dict[str, FaultType] = {
    # M4
    "payment_latency": FaultType(PaymentLatencyParams, _single("payment"), "faultpoint", "payment"),
    "payment_error": FaultType(PaymentErrorParams, _single("payment"), "faultpoint", "payment"),
    "service_unavailable": FaultType(NoParams, frozenset({"payment", "auth", "order"}), "container_stop"),
    # M5
    "cpu_saturation": FaultType(CpuSaturationParams, APP_SERVICES, "exec_cpu"),
    "memory_pressure": FaultType(MemoryPressureParams, APP_SERVICES, "exec_memory"),
    "db_slow_query": FaultType(DbSlowQueryParams, _single("postgres"), "pg_trigger", "postgres"),
    "db_connection_exhaustion": FaultType(NoParams, _single("postgres"), "pg_exhaustion", "postgres"),
    "db_lock_contention": FaultType(DbLockContentionParams, _single("postgres"), "pg_lock", "postgres"),
    "redis_latency": FaultType(RedisLatencyParams, _single("redis"), "redis_pause", "redis"),
    "redis_unavailable": FaultType(NoParams, _single("redis"), "container_stop", "redis"),
    "dependency_timeout": FaultType(NoParams, FAULTPOINT_SERVICES, "container_pause"),
    "intermittent_errors": FaultType(IntermittentErrorsParams, FAULTPOINT_SERVICES, "faultpoint"),
}

CONTAINER_MECHANISMS = frozenset({"container_stop", "container_pause"})
BACKGROUND_MECHANISMS = frozenset({"pg_exhaustion", "pg_lock", "redis_pause"})


def conflict_key(fault_type: str, target: str) -> tuple[str, str]:
    """Two active faults with the same key are mutually exclusive (409)."""
    mechanism = FAULT_TYPES[fault_type].mechanism
    if mechanism in CONTAINER_MECHANISMS:
        return ("container-state", target)  # a container can't be stopped and paused at once
    return (fault_type, target)
