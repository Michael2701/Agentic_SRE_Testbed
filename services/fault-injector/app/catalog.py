"""Fault types: allowed targets, parameter schema and the mechanism that realizes them."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaymentLatencyParams(_Params):
    latency_ms: int = Field(ge=1, le=30_000)
    jitter_ms: int = Field(default=0, ge=0, le=30_000)
    probability: float = Field(default=1.0, gt=0, le=1)


class PaymentErrorParams(_Params):
    status_code: int = Field(default=500, ge=500, le=599)
    probability: float = Field(default=1.0, gt=0, le=1)


class NoParams(_Params):
    pass


@dataclass(frozen=True)
class FaultType:
    params: type[_Params]
    targets: frozenset[str]
    mechanism: str  # "payment_hook" (app-level config push) | "container_stop" (Docker API)
    default_target: str | None = None


FAULT_TYPES: dict[str, FaultType] = {
    "payment_latency": FaultType(PaymentLatencyParams, frozenset({"payment"}), "payment_hook", "payment"),
    "payment_error": FaultType(PaymentErrorParams, frozenset({"payment"}), "payment_hook", "payment"),
    "service_unavailable": FaultType(NoParams, frozenset({"payment", "auth", "order"}), "container_stop"),
}
