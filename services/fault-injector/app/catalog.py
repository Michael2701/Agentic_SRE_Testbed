"""Fault types: allowed targets, parameter schema and the mechanism that realizes them."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

APP_SERVICES = frozenset({"gateway", "auth", "order", "payment"})
FAULTPOINT_SERVICES = frozenset({"auth", "order", "payment"})  # services with the libs/faultpoint hook
NETWORK_SERVICES = APP_SERVICES | {"postgres", "redis", "nginx"}
Service = Literal["gateway", "auth", "order", "payment", "postgres", "redis", "nginx"]

# Env variable holding the endpoint of each dependency, per client service (incorrect_endpoint).
DEPENDENCY_ENV = {
    "gateway": {"auth": "AUTH_URL", "order": "ORDER_URL"},
    "order": {"payment": "PAYMENT_URL", "postgres": "DATABASE_URL"},
    "auth": {"redis": "REDIS_URL"},
}
# Settings bad_configuration may change, per service (env names as in docker-compose.yml).
CONFIG_SETTINGS = {
    "gateway": frozenset({"HTTP_TIMEOUT_SECONDS"}),
    "order": frozenset({"HTTP_TIMEOUT_SECONDS", "DB_POOL_MIN_SIZE", "DB_POOL_MAX_SIZE"}),
    "auth": frozenset({"TOKEN_TTL_SECONDS"}),
}
BROKEN_APP_SPEC = "app.main:application"  # bad_deployment crash: the new release's entrypoint doesn't exist


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
    workers: int = Field(default=2, ge=1, le=32)


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


class _NetworkScope(_Params):
    peer: Service | None = None  # only traffic to this service; None = all egress of the target


class NetworkLatencyParams(_NetworkScope):
    delay_ms: int = Field(ge=1, le=10_000)
    jitter_ms: int = Field(default=0, ge=0, le=10_000)


class PacketLossParams(_NetworkScope):
    loss_percent: float = Field(default=20, gt=0, le=100)


class ConnectionFailureParams(_Params):
    peer: Service
    mode: Literal["reject", "drop"] = "reject"  # reject: TCP reset (refused); drop: packets vanish (timeout)


class IncorrectEndpointParams(_Params):
    dependency: Service
    endpoint: str | None = Field(default=None, min_length=1, max_length=256)  # default: typo in the host name


class IncorrectTimeoutParams(_Params):
    timeout_ms: int = Field(default=5, ge=1, le=60_000)  # clips the latency tail; ~20 stays latent


class BadConfigurationParams(_Params):
    settings: dict[str, str | int | float] = Field(min_length=1)


class BadDeploymentParams(_Params):
    version: str = Field(default="1.1.0", pattern=r"^[0-9A-Za-z._-]{1,32}$")
    defect: Literal["crash", "errors", "slow", "none"] = "errors"  # none: a harmless release (a decoy)
    error_rate: float = Field(default=0.5, gt=0, le=1)
    latency_ms: int = Field(default=800, ge=1, le=30_000)


class ProxyRateLimitParams(_Params):
    """A too-strict nginx limit_req: requests over `rate_rps` queue (up to `burst`), the rest get 503."""

    rate_rps: int = Field(default=8, ge=1, le=1000)
    burst: int = Field(default=20, ge=0, le=10_000)


class ProxyBandwidthLimitParams(_Params):
    """nginx `limit_rate` per response, e.g. a "100" typed instead of "100k": slow responses, no errors."""

    bytes_per_second: int = Field(default=100, ge=1, le=10_000_000)


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
    # netns           tc netem / iptables in the target's network namespace (network.py)
    # redeploy        container recreated with a changed env/command, like a new release (deploy.py)
    # edge_config     nginx config snippet pushed to the shared runtime dir + `nginx -s reload` (edge.py)
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
    # M6
    "network_latency": FaultType(NetworkLatencyParams, NETWORK_SERVICES, "netns"),
    "packet_loss": FaultType(PacketLossParams, NETWORK_SERVICES, "netns"),
    "connection_failure": FaultType(ConnectionFailureParams, frozenset(DEPENDENCY_ENV), "netns"),
    "incorrect_endpoint": FaultType(IncorrectEndpointParams, frozenset(DEPENDENCY_ENV), "redeploy"),
    "incorrect_timeout": FaultType(IncorrectTimeoutParams, frozenset({"gateway", "order"}), "redeploy"),
    "bad_configuration": FaultType(BadConfigurationParams, frozenset(CONFIG_SETTINGS), "redeploy"),
    "bad_deployment": FaultType(BadDeploymentParams, FAULTPOINT_SERVICES, "redeploy"),
    # M8
    "proxy_rate_limit": FaultType(ProxyRateLimitParams, _single("nginx"), "edge_config", "nginx"),
    "proxy_bandwidth_limit": FaultType(ProxyBandwidthLimitParams, _single("nginx"), "edge_config", "nginx"),
}

CONTAINER_MECHANISMS = frozenset({"container_stop", "container_pause", "redeploy"})
BACKGROUND_MECHANISMS = frozenset({"pg_exhaustion", "pg_lock", "redis_pause"})


def conflict_key(fault_type: str, target: str) -> tuple[str, str]:
    """Two active faults with the same key are mutually exclusive (409)."""
    mechanism = FAULT_TYPES[fault_type].mechanism
    if mechanism in CONTAINER_MECHANISMS:
        return ("container-state", target)  # a container can't be stopped, paused and redeployed at once
    if mechanism == "netns":
        return ("network", target)  # one qdisc / filter set per network namespace
    return (fault_type, target)


def check_target(fault_type: str, target: str, params: dict) -> str | None:
    """Validation that depends on the target; returns an error message or None."""
    peer = params.get("peer")
    if peer == target:
        return "peer must differ from target"
    if fault_type == "incorrect_endpoint" and params["dependency"] not in DEPENDENCY_ENV[target]:
        return f"dependency of {target} must be one of {sorted(DEPENDENCY_ENV[target])}"
    if fault_type == "bad_configuration":
        unknown = set(params["settings"]) - CONFIG_SETTINGS[target]
        if unknown:
            return f"settings of {target} must be among {sorted(CONFIG_SETTINGS[target])}; got {sorted(unknown)}"
    return None


def _typo(url: str) -> str:
    """http://payment:8000 -> http://payments:8000 (the host no longer resolves)."""
    parts = urlsplit(url)
    userinfo, at, hostport = parts.netloc.rpartition("@")
    host, colon, port = hostport.partition(":")
    return urlunsplit(parts._replace(netloc=f"{userinfo}{at}{host}s{colon}{port}"))


def deployment(fault: dict, baseline_env: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Env overrides of a redeploy fault, and whether the new release's entrypoint is broken."""
    params = fault["parameters"]
    if fault["type"] == "incorrect_endpoint":
        key = DEPENDENCY_ENV[fault["target"]][params["dependency"]]
        return {key: params["endpoint"] or _typo(baseline_env[key])}, False
    if fault["type"] == "incorrect_timeout":
        return {"HTTP_TIMEOUT_SECONDS": str(params["timeout_ms"] / 1000)}, False
    if fault["type"] == "bad_configuration":
        return {key: str(value) for key, value in params["settings"].items()}, False
    return {"SERVICE_VERSION": params["version"]}, params["defect"] == "crash"  # bad_deployment
