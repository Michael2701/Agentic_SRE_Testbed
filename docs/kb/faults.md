# Fault injection (M4 foundation, M5 advanced faults)

## Rule: control plane ≠ diagnostic plane
The injector knows the ground truth. Diagnostic telemetry (Loki, Prometheus, Tempo) may show **only
symptoms**:
- fault-injector has no `instrument()`, no `/metrics`, no traces. Alloy **drops** its logs
  (`discovery.relabel` rule, action `drop`), and Prometheus doesn't scrape it.
- Control endpoints in services use the `/__` prefix. `libs/observability` excludes `/__*` from access
  logs, RED metrics (`is_unobserved()` in `middleware.py`) and traces (`EXCLUDED_URLS` in `tracing.py`).
- Hook code never logs, counts or annotates anything saying "fault". Faulted responses look like real
  ones: `libs/faultpoint` resolves `scope["route"]` before short-circuiting, otherwise telemetry would show
  `route="unmatched"` (a tell-tale sign).
- Mechanism artifacts have neutral names: trigger `orders_write_hook`, role `reporting`, pidfiles
  `/tmp/.w-<id>-<n>.pid`. `test_no_mechanism_names_in_logs` checks that Loki never contains them.
- Verified by `test_control_plane_invisible_to_diagnostic_plane`.
- ⚠️ If `alloy/config.alloy` changes while the stack is running, logs shipped before the Alloy restart stay
  in Loki. Loki has no volume, so `make down && make up` gives a clean slate.

## Service `services/fault-injector` (control plane, host `127.0.0.1:8090`)
- Built with context `./services/fault-injector`, **without** `libs/observability`. Plain JSON logs go to
  docker stdout only.
- API: `POST/GET /faults`, `GET/DELETE /faults/{id}`, `DELETE /faults`, `GET /health`. Returns
  201 created, 409 for a duplicate active (type, target), 422 for validation errors, and 502 when a
  fault can't be applied (stored `failed`) or reverted (stays `active` with `error`).
- Model: `id` `flt-<hex>`, `experiment_id` (`[A-Za-z0-9._-]{1,64}`, default `adhoc-<hex>`), `type`,
  `target`, `parameters` (validated and defaults filled), `state` active|removed|failed, `error`,
  `created_at`, `updated_at`. Removed faults stay as history.
- Files: `app/catalog.py` (types: param schemas with `extra="forbid"`, targets, mechanism),
  `app/store.py` (SQLite `/data/faults.db` on volume `faultdata`, stdlib), `app/docker_api.py`
  (Docker Engine API over the unix socket via httpx; container found by compose labels),
  `app/reconcile.py` (`enforce`, `revert`), `app/main.py` (API + reconcile loop every 2s,
  one `asyncio.Lock` for all mutations).

## Types (`services/fault-injector/app/catalog.py`)
| type | target | params | mechanism | revert |
|---|---|---|---|---|
| `payment_latency` | payment | `latency_ms`, `jitter_ms`=0, `probability`=1 | faultpoint | config cleared |
| `payment_error` | payment | `status_code`=500, `probability`=1 | faultpoint | config cleared |
| `intermittent_errors` | auth, order, payment | `error_rate`=0.3, `status_code`=500 | faultpoint | config cleared |
| `service_unavailable` | payment, auth, order | — | `docker stop` | start + wait healthy |
| `redis_unavailable` | redis | — | `docker stop` | start + wait healthy |
| `dependency_timeout` | auth, order, payment | — | `docker pause` (TCP accepted, no reply → caller read timeout) | unpause + wait healthy |
| `cpu_saturation` | gateway, auth, order, payment | `workers` 1–8 (=2) | busy-loop python processes via docker exec | kill pids |
| `memory_pressure` | same | `mb` 16–2048 (=200) | a python process allocating and touching `mb` MiB | kill pid |
| `db_slow_query` | postgres | `delay_ms`, `operations` ⊆ {insert, update} | `BEFORE` trigger `orders_write_hook` with `pg_sleep` | drop trigger + function |
| `db_connection_exhaustion` | postgres | — | role `reporting` (non-superuser) fills every normal slot; `pg_terminate_backend` evicts `order_svc` | close connections |
| `db_lock_contention` | postgres | `hold_ms`=1500, `interval_ms`=2000 | loop: `LOCK TABLE orders IN SHARE MODE` for hold_ms | cancel (conn close releases) |
| `redis_latency` | redis | `pause_ms`, `interval_ms`=1000 | loop: `CLIENT PAUSE pause_ms ALL` | cancel + `CLIENT UNPAUSE` |

- **Mechanism modules:** `docker_api.py` (stop/pause/exec/resume), `workers.py` (exec hogs: pidfile check
  every pass, relaunch if the container restarted or OOM killed the hog), `datastores.py` (Postgres
  trigger/exhaustion/lock, Redis pause), `reconcile.py` (`enforce` idempotent; background tasks
  supervised per fault id and restarted if they die).
- **Conflicts** (`catalog.conflict_key`): same (type, target); a container can't be both stopped and paused.
- **faultpoint** (`libs/faultpoint`, used by auth, order, payment): pure ASGI middleware + `PUT/DELETE/GET
  /__faults`. Probes and `/__*` are exempt. **`faultpoint.install(app)` must come before
  `observability.instrument(app)`** so the service's own access log/RED metrics record injected
  latency/5xx. Multiple error faults on one service: the highest probability wins.
- Periodic params: `interval_ms` must be > `hold_ms`/`pause_ms` (validator).
- Connection budget: `max_connections=40`, 3 reserved for superusers (exporter, injector admin,
  exhaustion admin). The app uses the non-superuser `order_svc`, so exhaustion is deterministic while
  operators still get in.

## Adding a fault type (M6+)
1. Add a params model (`extra="forbid"`) + `FaultType` (targets, mechanism) to `catalog.py`.
2. Implement the mechanism: `enforce` must be idempotent (it runs every 2s and must not take locks or
   do heavy work when the fault is already in place); add `revert`. Long-running loops belong in
   `BACKGROUND_MECHANISMS` (supervised tasks).
3. Use neutral names for anything visible to the diagnostic plane.
4. Add symptom + evidence + recovery tests (`tests/faults/test_advanced_faults.py` pattern).
5. Prefer real mechanisms (containers, network) over `sleep()` (`project.md` M6).

## Make targets
`make fault TYPE=payment-latency PARAMS='{"latency_ms":2000}' [TARGET=...] [EXP=...]` (kebab → snake),
`make faults` (active), `make recover` (DELETE all), `make test-faults` (only fault tests, ~3 min).
The host uses curl against `localhost:8090`.

## Symptom → possible root causes (M5 DoD, verified by tests; seed for M8)
| external symptom on `POST /orders` | root causes that produce it | distinguishing evidence |
|---|---|---|
| slow, still 201 | payment_latency | order→payment dependency latency; slow payment server span |
| | db_slow_query | slow postgres INSERT/UPDATE spans; payment edge normal |
| | db_lock_contention | `pg_locks_count{mode="sharelock"}`; periodic spikes (hold/interval), not constant |
| | redis_latency | auth→redis latency (auth `dependency_*{dependency="redis"}`); `/validate` slow |
| | cpu_saturation(order) | `container_cpu_throttled_seconds_total`, CPU PSI of order |
| 5xx | payment_error / intermittent_errors | 5xx at payment itself (its RED metrics); 502 from order |
| | db_connection_exhaustion | **500** from order; `pg_stat_activity_count{usename="reporting"}` ≈ limit |
| | redis_unavailable | 502 from gateway (auth 500 on validate); `redis_up`=0 |
| | dependency_timeout(payment) | ~5s then **504** (gateway and order timeouts are both 5s: gateway gives up first); `outcome="timeout"` |
| | service_unavailable | fast 502; `outcome="error"` (connection refused) |
| none externally | memory_pressure | `container_memory_usage_bytes` near limit, memory PSI, possible OOM kills |

Probe gotcha: periodic faults (lock, redis pause) hit only requests that land in the hold window. A
burst of sequential requests fits into one free window and looks healthy, so sample over time (tests
use 250 ms spacing).

## Observed incidents, M4 (5 rps load, 30s windows)
| fault | symptom |
|---|---|
| payment_latency 2000ms | gateway `POST /orders` p95 23ms → 2.4s; 0 errors |
| payment_error | 5xx ratio 0 → 64%, order→payment errors and `orders_total{payment_failed}` ~4/s |
| service_unavailable(payment) | 5xx ratio → 52%, order→payment outcome=error ~3.8/s |
All recover to baseline after removal.
