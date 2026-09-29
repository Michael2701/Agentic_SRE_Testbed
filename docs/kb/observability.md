# Observability (M2)

## Shared library `libs/observability` (telemetry only, no business logic)
- `instrument(app, service)` in `__init__.py` sets up JSON logging, adds `ObservabilityMiddleware`
  and `GET /metrics`. Every service calls it right after `app = FastAPI(...)`.
- `context.py`: `request_id_var` (contextvar) and `accept_or_generate()`, which keeps well-formed
  incoming IDs (`[A-Za-z0-9._-]{1,128}`) and otherwise uses uuid4 hex.
- `middleware.py`: pure ASGI middleware. It sets the request ID, echoes `x-request-id` in the response,
  and records `http_requests_total{method,route,status}` + `http_request_duration_seconds{method,route}`.
  `route` is the FastAPI template from `scope["route"].path` (or `unmatched`). It writes one `request`
  log line per request (5xx → level error). `/health`, `/ready` and `/metrics` are skipped.
- `logging.py`: `JsonFormatter`. Any `extra={...}` key becomes a top-level field; `request_id` is
  added from the contextvar and `trace_id`/`span_id` from the current OTel span (M3, see
  [tracing.md](tracing.md)). uvicorn loggers are re-routed, `uvicorn.access` is disabled (also
  `--no-access-log`), and `httpx` is set to WARNING.
- `metrics.py`: `track(dependency, operation)` async context manager for non-HTTP deps
  (redis `set_token`/`get_token`; postgres `insert_order`/`update_order_status`/`get_order`).
  Outcome is `success|timeout|error`.
- `http.py` (extra `[http]`): `instrumented_client(base_url, dependency, timeout)` wraps
  `InstrumentedTransport`. It injects `X-Request-ID`, records `dependency_*` metrics with operation
  `"METHOD /path"` (IDs collapsed to `{id}`), treats HTTP ≥500 as `error`, and logs
  `dependency_call_failed` (warning) on non-success.
- Service name = Prometheus `job` label; metrics carry no `service` label.
- Histogram `BUCKETS` start at 1ms (1, 2.5, 5, 7.5, 10, 25 ms …). With a 5ms first bucket, auth and payment
  (both <1ms) had an identical p95 of ~4.75ms. Baseline p95 at 10 rps: gateway POST /orders ≈23ms,
  order ≈7ms, auth ≈2ms, payment ≈1ms. This baseline is useful for judging fault impact later.
- Business metrics are defined in each service: `logins_total{result}`,
  `token_validations_total{result}`, `orders_total{status}`, `payments_total{status}`.

## Stack (docker-compose)
- Prometheus v3.5 (host :9090, 5s scrape, `prometheus/prometheus.yml`; jobs gateway, auth, order,
  payment, nginx, postgres, redis, prometheus).
- Loki 3.5 (internal :3100, filesystem/tsdb, no retention config, no healthcheck: the image has no wget).
- Alloy v1.10 (`alloy/config.alloy`): docker discovery filtered by label
  `com.docker.compose.project=sre-testbed`, **refresh_interval 5s** (the 60s default missed containers
  started after Alloy). Labels are `service`, `container` and `level` (from JSON). request_id is NOT a
  label; query it with `|= "<id>"` or `| json | request_id="..."`.
- Grafana 12.1 (host :3000, anonymous Admin, login form off). Datasource uids are `prometheus` and
  `loki`. Dashboards are in `grafana/dashboards/*.json` with uids `sre-overview`, `sre-dependencies`,
  `sre-logs`, folder "SRE Testbed". The home dashboard is Service Overview.
- Exporters: nginx-exporter (→ `nginx:8081/stub_status`, internal server block), postgres-exporter,
  redis-exporter.
- **cAdvisor was removed** (it can't map cgroups on Docker Desktop's containerd store). **Since M5,
  container metrics come from `libs/observability/cgroup.py`:** each app service reads its own cgroup v2
  files, covering all processes in the container (including fault hogs):
  - `container_cpu_usage_seconds_total`, `container_cpu_throttled_seconds_total`,
    `container_cpu_throttled_periods_total`, `container_cpu_limit_cores`;
  - `container_memory_usage_bytes`, `container_memory_limit_bytes`, `container_oom_kills_total`;
  - PSI `container_{cpu,memory}_pressure_stalled_seconds_total`.

  These metrics are unlabelled; the service is the `job` label. Postgres/Redis still rely on their exporters.

## Dashboards
- **Source of truth: `grafana/generate_dashboards.py`** (stdlib only). Edit it, run
  `python3 grafana/generate_dashboards.py`, and commit both. Don't hand-edit `grafana/dashboards/*.json`.
  Keep to one unit per panel (no dual axes), stat tiles for headlines, green/yellow/red only for thresholds.
  All dashboards are tagged `sre-testbed` and linked to each other via the tag link.
- Error panels use `... or <same grouping> * 0` / `or vector(0)` so healthy = 0 rather than "No data".
- To check that panels have data: evaluate each target `expr` against Prometheus
  (`localhost:9090/api/v1/query`) and Loki (via `localhost:3000/api/datasources/proxy/uid/loki/...`).

## Traffic
- `make load RATE=5 DURATION=60` runs `tests/load.py` in the tests container through nginx. The mix is
  ~80% POST /orders, ~15% GET own orders, ~5% bad logins (401 as background noise).
