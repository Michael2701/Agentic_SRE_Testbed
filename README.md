# Agentic_SRE_Testbed

A small, production-like distributed system that will later be broken in controlled,
reproducible ways to train and evaluate a multi-agent SRE system. See `project.md` for the full roadmap.

**Current state: Stage 0 complete (Milestone 9 — Stage 0 stabilization)** (see [Stage 0 status](#stage-0-status)): one-command
clean reset, a smoke check, an acceptance run of every scenario, a recovery sweep over every fault type and a
per-domain dashboard. M8 added deliberately hard scenarios: eight
different causes with one identical symptom, a misleading deployment before the real cause, and
"model-breaking" incidents where every dashboard domain looks normal. Reproducible experiments run a
scenario end to end (baseline → inject → observe → record → remove → verify recovery) under generated
traffic and store the hidden ground truth. They build on a Fault Injector with 22 controlled, reversible faults: CPU, memory,
database, Redis, dependencies (M5), network, configuration and deployments (M6). Different root causes
produce similar external symptoms. Built on M3 tracing (OpenTelemetry → Tempo) and M2 observability
(JSON logs, request IDs, Prometheus, Loki, Grafana).

## Architecture

```text
                         Redis
                           ↑
                           |
Client → Nginx → Gateway → Auth Service
                  |
                  └────→ Order Service → PostgreSQL
                              |
                              ↓
                       Payment Simulator
```

| Component | Tech | Role |
|---|---|---|
| nginx | nginx 1.27 | Public entry point (`localhost:8080`), reverse proxy to gateway |
| gateway | FastAPI | Routes requests, validates tokens via auth, forwards to order |
| auth | FastAPI + Redis | Fake login, issues opaque tokens stored in Redis with TTL |
| order | FastAPI + PostgreSQL | Persists orders, charges them via payment |
| payment | FastAPI | Stateless payment simulator, always approves |
| postgres | PostgreSQL 16 | `orders` table (idempotent migrations in `db/migrations/`, applied by `db-migrate`) |
| redis | Redis 7 | Token store (`token:<token> → user_id`) |

Observability stack:

| Component | Role | Host port |
|---|---|---|
| Prometheus | Scrapes app `/metrics` and exporters every 5s | `9090` |
| Loki | Log storage | — |
| Tempo | Trace storage (OTLP), service graph + span metrics → Prometheus | — |
| Grafana Alloy | Tails container logs via the Docker socket, parses JSON, ships to Loki | — |
| Grafana | Provisioned datasources + dashboards, anonymous admin | `3000` |
| nginx / postgres / redis exporters | Infrastructure metrics | — |

Everything else lives on the internal `backend` network.

## Requirements

- Docker with Compose v2
- GNU Make

## Usage

```bash
make up     # build and start everything, waits until all containers are healthy
make reset  # clean slate: revert faults, wipe all state (DB, fault/experiment history, telemetry), start, smoke
make smoke  # ~5 s health check: order 201, no active faults, telemetry backends reachable
make test   # run integration tests (in a container, through nginx)
make load   # steady traffic, e.g. make load RATE=10 DURATION=120
make fault TYPE=payment-latency PARAMS='{"latency_ms":2000}'   # inject a fault
make faults     # list active faults
make recover    # remove all faults
make test-faults   # only the fault tests (~5 min)
make scenarios                           # list experiment scenarios
make experiment SCENARIO=payment-latency # run one experiment (~75s) and print the verdict
make experiments                         # recorded experiments, with ground truth
make test-experiments                    # only the experiment tests (~12 min, incl. challenges)
make test-challenges                     # only the M8 challenge scenarios (~10 min)
make acceptance                          # every scenario end to end, shortened phases (~30 min)
make acceptance ROUNDS=2 FULL=1 SCENARIOS="slow-redis slow-cpu"   # repeat / full durations / a subset
make ps     # container status
make logs   # follow logs
make down   # stop the environment
```

Configuration defaults live in `docker-compose.yml`; override them by copying `.env.example` to `.env`.

Demo users (fake auth by design): `alice:alice`, `bob:bob`.

## Request flow

```text
POST /login   → gateway → auth → Redis (store token)
POST /orders  → gateway → auth /validate (Redis) → order → INSERT pending
                                                       → payment /payments
                                                       → UPDATE paid → 201
```

If the payment call fails, the order is stored as `payment_failed` and the API returns `502`.

### Example session

```bash
TOKEN=$(curl -s -XPOST localhost:8080/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"alice"}' | jq -r .access_token)

curl -s -XPOST localhost:8080/orders -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"item":"book","quantity":1,"amount_cents":1500}'

curl -s localhost:8080/orders/<order-id> -H "Authorization: Bearer $TOKEN"
```

## Observability

- **Logs:** every service writes one JSON object per line to stdout:
  `ts, level, service, logger, msg, request_id` plus event fields. Examples are `order_created`,
  `payment_approved`, `login_failed` and `dependency_call_failed`, plus one `request` access line per
  request. Probe and scrape paths (`/health`, `/ready`, `/metrics`) are not logged.
- **Request ID:** nginx keeps a client `X-Request-ID` or generates one. Every service propagates it on
  outbound calls and returns it in the response. To follow a request, open the **Logs** dashboard and
  paste the ID, or query Loki with `{service=~".+"} |= "<id>"`.
- **Metrics:** each app service exposes `/metrics`:
  - `http_requests_total{method,route,status}` and `http_request_duration_seconds` (RED);
  - `dependency_requests_total{dependency,operation,outcome}` and `dependency_request_duration_seconds`
    (every outbound call to auth/order/payment/postgres/redis);
  - business counters `logins_total`, `token_validations_total`, `orders_total`, `payments_total`.

  - `app_build_info{version}`: the running release (env `SERVICE_VERSION`, default `1.0.0`; also the
    `version` log field and the `service.version` span resource attribute).

  The service name is the Prometheus `job` label.
- **Dashboards** (Grafana → folder *SRE Testbed*):
  - *Service Overview*: RED, order-flow latency, business counters, process and container resources (CPU, throttling, memory, PSI), releases (running version, process uptime);
  - *Dependencies*: every dependency edge, plus PostgreSQL (connections by role, locks) and Redis;
  - *Logs*: filter by service, level and request_id.
  - *Traces*: service map, recent and slow `POST /orders` traces, per-edge metrics from spans;
  - *Diagnostic domains*: the order symptom plus the signals the experiment-runner judges per domain
    (application, database, redis, payment). There is no proxy domain on purpose (model-breaking scenarios).

  Dashboards are generated by `grafana/generate_dashboards.py`: edit it and re-run it rather than editing the JSON.
- Shared telemetry code lives in `libs/observability` and is installed into every service image.

## Tracing

Every request is traced end to end (100% sampling):

```text
nginx (root) → gateway ─┬→ auth → Redis
                        └→ order ─┬→ PostgreSQL
                                  └→ payment
```

- nginx (`ngx_otel_module`) starts the trace, propagates W3C `traceparent` and returns the trace ID in
  the `X-Trace-ID` response header.
- Python services use OpenTelemetry auto-instrumentation for FastAPI, httpx, asyncpg and redis. Spans
  carry business attributes: `user.id`, `order.id`, `order.status`, `payment.id`.
- All JSON logs include `trace_id` and `span_id`. In Grafana you can jump from a log line (Loki) to its
  trace (Tempo) and from a span to the logs of that service.

To follow one request:

```bash
curl -si -XPOST localhost:8080/orders -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"item":"book","quantity":1,"amount_cents":1500}' | grep -i x-trace-id
```

Then open Grafana → Explore → Tempo and paste the trace ID, or use the *Traces* dashboard.

## Fault injection

`fault-injector` is the **control plane**. It knows what was broken; the telemetry stack must not. It has
no metrics or traces, and its logs are not shipped to Loki. Services' internal fault hooks (`/__*`) are
excluded from logs, metrics and traces, so diagnosis sees only symptoms.

API (`http://localhost:8090`, bound to localhost only):

| Method | Path | Description |
|---|---|---|
| POST | `/faults` | `{type, target?, parameters?, experiment_id?}` → 201 (409 duplicate, 422 invalid) |
| GET | `/faults` | all faults, `?state=active\|removed\|failed` |
| GET | `/faults/{id}` | one fault |
| DELETE | `/faults/{id}` | remove (revert) one fault; it stays in history as `removed` |
| DELETE | `/faults` | remove all active faults |

Each fault has `id`, `experiment_id`, `type`, `target`, `parameters`, `created_at`/`updated_at` and `state`.

| Type | Target | Parameters | Mechanism (real where practical) |
|---|---|---|---|
| `payment_latency` | payment | `latency_ms`, `jitter_ms`, `probability` | app hook delays responses |
| `payment_error` | payment | `status_code` (5xx), `probability` | app hook returns 5xx |
| `intermittent_errors` | auth, order, payment | `error_rate`, `status_code` | app hook returns 5xx for a share of requests |
| `service_unavailable` | payment, auth, order | — | container stopped (connection refused) |
| `redis_unavailable` | redis | — | container stopped |
| `dependency_timeout` | auth, order, payment | — | container paused (connection accepted, no reply) |
| `cpu_saturation` | gateway, auth, order, payment | `workers` | busy processes inside the container, which has a 0.5 CPU quota |
| `cpu_limit` | same | `cpus` | the container's CPU quota lowered in place (`docker update --cpus`), no restart |
| `memory_pressure` | same | `mb` | a process allocating memory inside the container (256 MiB limit) |
| `db_slow_query` | postgres | `delay_ms`, `operations` | trigger with `pg_sleep` on INSERT/UPDATE |
| `db_connection_exhaustion` | postgres | — | another role holds every normal connection slot |
| `db_lock_contention` | postgres | `hold_ms`, `interval_ms` | periodic `LOCK TABLE orders IN SHARE MODE` |
| `redis_latency` | redis | `pause_ms`, `interval_ms` | periodic `CLIENT PAUSE` |
| `network_latency` | app services, postgres, redis | `delay_ms`, `jitter_ms`, `peer`? | `tc netem delay` in the target's network namespace (only towards `peer` if given) |
| `packet_loss` | same | `loss_percent`, `peer`? | `tc netem loss` |
| `connection_failure` | gateway, auth, order | `peer`, `mode` (reject\|drop) | `iptables` rejects (TCP reset) or drops traffic to the peer |
| `incorrect_endpoint` | gateway, order, auth | `dependency`, `endpoint`? | redeploy with a wrong dependency URL (default: host name typo) |
| `incorrect_timeout` | gateway, order | `timeout_ms` (=5) | redeploy with a too-short HTTP timeout |
| `bad_configuration` | gateway, order, auth | `settings` (allowlisted env vars) | redeploy with e.g. `DB_POOL_MAX_SIZE=1`, `TOKEN_TTL_SECONDS=2` |
| `bad_deployment` | auth, order, payment | `version`, `defect` (crash\|errors\|slow), `error_rate`, `latency_ms` | redeploy as a new version that crash-loops, returns 5xx or is slow |

Network faults run `tc`/`iptables` from a short-lived helper container that shares the target's network
namespace; app images are unchanged. Redeploy faults recreate the target container (same name, image and
network aliases) with a changed environment, like a real rollout; removing the fault redeploys the original.

Similar symptoms, different causes (verified by `tests/faults/test_advanced_faults.py` and `test_infra_faults.py`):

| Symptom on `POST /orders` | Root causes |
|---|---|
| slow, still 201 | payment_latency, db_slow_query, db_lock_contention, redis_latency, cpu_saturation, network_latency, packet_loss (tail), bad_deployment slow |
| 5xx | payment_error, intermittent_errors, db_connection_exhaustion, redis_unavailable, dependency_timeout, service_unavailable, connection_failure, incorrect_endpoint, incorrect_timeout, bad_deployment crash/errors |
| 401 | bad_configuration (short token TTL) |
| nothing visible externally | memory_pressure (only container memory metrics); bad_configuration pool=1 until the DB is slow |

Each cause leaves different evidence (dependency latencies, spans, container throttling, `pg_locks`,
connection counts, timeout vs. connection-error outcomes, a healthy peer, DNS errors, a new release version
and process restart). See `docs/kb/faults.md`.

Faults are persisted (SQLite) and re-applied every 2s, so they survive restarts of the injector or the
target. Removing a fault reverts it; for `service_unavailable` the container is started and the injector
waits until it is healthy.

```bash
make load RATE=5 DURATION=180 &
make fault TYPE=payment-latency PARAMS='{"latency_ms":2000}' EXP=exp-1
# watch Grafana: Service Overview p95, Traces (slow order → payment)
make recover
```

## Experiments

`experiment-runner` (`http://localhost:8091`, localhost only) is control plane as well: it drives the
system only through the fault-injector API and nginx, and its logs and metrics never reach the telemetry
stack. A scenario (`experiments/scenarios/<name>.json`) names the faults, traffic rate, phase durations and
the expected symptom (`slow`, `errors`, `auth_errors` or `none`).

```text
baseline → inject → observe → record → remove → recovery
(traffic runs throughout; the runner measures every request per phase)
```

```bash
$ make experiment SCENARIO=payment-latency
exp-1  scenario=payment-latency  state=completed
ground truth: payment_latency on payment {"latency_ms": 1500, "jitter_ms": 0, "probability": 1.0}
baseline  p50=0.013s p95=0.025s errors=0% 401=0% rps=4.66
observe   p50=1.515s p95=1.526s errors=0% 401=0% rps=4.83
verdict: expected=slow observed=['slow'] expected_seen=True recovered=True recovery_seconds=0.0
```

Each record keeps `ground_truth` (the injected faults: hidden, control plane only) apart from `incident`
(time window and observed symptoms, without the cause), plus per-phase client stats, a Prometheus snapshot
and the verdict. Only one experiment runs at a time, and none starts while faults are active.

| Method | Path | Description |
|---|---|---|
| GET | `/scenarios` | scenario files, each validated against the fault catalog |
| POST | `/experiments` | `{scenario, overrides?}` → 202; `?wait=true` blocks until done (409 busy/faults active) |
| GET | `/experiments`, `/experiments/{id}` | records (`?format=text` for a summary) |
| DELETE | `/experiments/{id}` | abort: stop traffic, remove its faults |

Starter scenarios: payment-latency, payment-error, db-slow-query, redis-unavailable, network-latency,
cpu-saturation, connection-failure, bad-deployment. See `docs/kb/experiments.md`.

### Challenge scenarios (M8)

| Type | Scenarios | What makes it hard |
|---|---|---|
| Same symptom | slow-payment, slow-db-query, slow-pool-exhaustion, slow-redis, slow-cpu, slow-network, slow-proxy-network, slow-proxy-config | all give POST /orders p95 > 2 s with 201s at the same traffic; only the evidence differs |
| Misleading correlation | deploy-then-payment | a harmless order release, then 5 minutes later payment slows down; rolling back the release changes nothing |
| Model-breaking | slow-proxy-network, slow-proxy-config | application, database, Redis and payment all look NORMAL; the delay is in nginx, visible only in its logs and traces |

Each record also carries a per-domain view (application, database, redis, payment: NORMAL/DEGRADED from
Prometheus), the incident onset (when symptoms began, not when faults went in) and whether the view
matched the evidence the scenario declares. Proxy faults: `network_latency`/`packet_loss` with target
`nginx`, `proxy_bandwidth_limit` and `proxy_rate_limit` (nginx config snippets + reload). See
`docs/kb/challenges.md`.

## Stage 0 status

Every point of the Stage 0 definition of done (`project.md`) and where it is proven:

| # | Requirement | Evidence |
|---|---|---|
| 1 | Environment starts with one command | `make up` (waits for healthy), `make reset` for a clean slate, `make smoke` |
| 2 | Normal `/orders` traffic works reliably | `test_happy_path.py`, `make load`, the baseline phase of every experiment |
| 3 | Metrics, logs and traces expose behaviour | `test_observability.py`, `test_tracing.py`, five Grafana dashboards |
| 4 | ≥ 8 fault scenarios work | 17 scenario files, `make acceptance` (all pass, see `docs/kb/stage0.md`) |
| 5 | Different causes, similar symptoms | 8 same-symptom scenarios (`test_challenges.py`), symptom → cause table in `docs/kb/faults.md` |
| 6 | Hidden ground truth recorded | `ground_truth` vs `incident` in every record (`test_experiments.py`) |
| 7 | Faults are reversible | `test_recovery_sweep.py` (all 22 types), `make recover` |
| 8 | Experiments are reproducible | `make acceptance ROUNDS=2`: the same verdict in each round |
| 9 | Telemetry divided into diagnostic domains | per-domain view in every record, *Diagnostic domains* dashboard |
| 10 | ≥ 2 model-breaking experiments | slow-proxy-network, slow-proxy-config |
| 11 | ≥ 1 misleading-correlation experiment | deploy-then-payment |
| 12 | Integration and E2E tests pass | `make test` |
| 13 | Setup and execution documented | this README, `docs/kb/` |

## Troubleshooting

- **Something is left over** (an experiment aborted, the injector restarted mid-fault): `make recover`, then
  `make smoke`. If it still fails, `make reset` (wipes all state and starts from scratch, ~1 min).
- **A few 502s right after a redeploy fault or its removal**: callers keep a stale keep-alive connection or
  DNS entry (nginx resolver `valid=5s`) for a moment. Tests poll past it; it is expected.
- **Proxy faults fail with `nginx reload failed`**: nginx must read the current `nginx/nginx.conf`. The
  directory is mounted, not the file: a single-file bind mount keeps the old inode when git or an editor
  replaces the file, and the reload then finds no config. If you changed the mount, `make reset`.
- **`make experiment` answers 409**: another experiment is running or faults are active (`make faults`).
- **Another machine**: scenarios whose fault sets a duration (payment, DB, network, Redis, proxy) behave
  the same anywhere. CPU starvation depends on how fast the host CPU is, so slow-cpu doesn't use a fixed
  quota: the runner measures order's CPU per request during the baseline and sets the quota to a share of
  what the scenario's load needs (`baseline_relative`). On a new host, `make acceptance` shows in ~30 min
  whether every scenario still holds.

## API

Public (via nginx, `http://localhost:8080`):

| Method | Path | Description |
|---|---|---|
| POST | `/login` | `{username, password}` → `{access_token, token_type, expires_in}`; 401 on bad credentials |
| POST | `/orders` | Bearer token; `{item, quantity, amount_cents, currency?}` → 201 order with `status: "paid"` |
| GET | `/orders/{id}` | Bearer token; returns the caller's order, 404 otherwise |
| GET | `/health` | Gateway liveness |
| GET | `/ready` | Gateway readiness (checks auth and order `/ready`) |
| GET | `/nginx-health` | Nginx liveness |

All responses carry `X-Request-ID`.

Internal:

| Service | Endpoints |
|---|---|
| auth | `POST /login`, `POST /validate` (Authorization header → `{user_id}`), `GET /health`, `GET /ready` (Redis ping) |
| order | `POST /orders`, `GET /orders/{id}` (both require `X-User-Id`), `GET /health`, `GET /ready` (`SELECT 1`) |
| payment | `POST /payments` → `{payment_id, status: "approved", ...}`, `GET /health` |

Every app service also serves `GET /metrics`.

Downstream transport failures at the gateway map to `502` (unavailable) or `504` (timeout).

## Tests

Tests run inside the compose network:

- `test_happy_path.py` goes through nginx and covers health/readiness, login success and failure, the full
  order happy path (create → read back from PostgreSQL), and rejection of missing or invalid tokens.
- `test_observability.py` covers:
  - request IDs are generated and preserved;
  - `/metrics` on every service, and business and dependency counters grow;
  - all Prometheus targets are `up`;
  - one request ID is found in Loki logs of nginx, gateway, auth, order and payment;
  - Grafana datasources are healthy and the dashboards are provisioned.
- `test_tracing.py` covers:
  - one `/orders` trace has a single nginx root, the parent-child chain above, Redis and PostgreSQL
    spans, and business attributes;
  - its trace ID appears in the logs of every service;
  - Grafana reaches Tempo.
- `tests/faults/test_advanced_faults.py` covers the M5 DoD: for each root cause it checks the shared
  symptom (slow or 5xx), its specific evidence in telemetry, and recovery.
- `tests/faults/test_infra_faults.py` covers M6: each network, configuration and deployment fault with its
  symptom, distinguishing evidence and recovery, plus re-application after a target restart.
- `tests/experiments/test_experiments.py` covers M7: the full lifecycle with shortened phases, ground truth
  linked to the injector's faults, an incident record without the cause, one experiment at a time, abort,
  and the runner staying invisible to the telemetry stack.
- `tests/experiments/test_challenges.py` covers M8: every challenge scenario end to end (shared symptom,
  per-domain evidence, model-breaking domains all NORMAL, rollback not fixing the misleading incident).
- `tests/faults/test_proxy_faults.py` covers the proxy faults and the harmless release.
- `tests/faults/test_recovery_sweep.py` covers M9: every one of the 22 fault types is injected and removed,
  and the system must return to its pre-fault state (orders 201 at baseline latency, same releases, same token TTL).
- `tests/faults/test_faults.py` runs after the integration tests and recovers after each test. It covers:
  - the fault API (lifecycle, validation, conflicts, delete-all);
  - the incident and the recovery for each fault type (service_unavailable for payment, auth and order);
  - the reconcile loop re-applying a lost fault;
  - that the control plane leaves no trace in Loki, Prometheus or Tempo.

## Repository layout

```text
nginx/nginx.conf          reverse proxy config (JSON access log, request IDs, stub_status on :8081); the dir is mounted
libs/observability/       shared telemetry package: JSON logging, request-ID middleware, metrics, httpx transport
services/<name>/          one FastAPI service per directory (Dockerfile, requirements.txt, app/)
services/fault-injector/  control plane: fault API, SQLite store, Docker API, Postgres/Redis, network (tc/iptables) and redeploy mechanisms, reconcile loop
libs/faultpoint/          app-level fault hook (latency / 5xx) used by auth, order, payment
db/migrations/            idempotent SQL (schema, roles), applied by the db-migrate service
prometheus/ loki/ alloy/  observability stack configs
tempo/                    trace backend config
grafana/                  provisioning, dashboards/*.json and generate_dashboards.py (their source)
tests/integration/        pytest integration suite (runs via `make test`)
services/experiment-runner/  control plane: scenarios, traffic, experiment lifecycle and records (SQLite)
experiments/scenarios/    scenario files (JSON)
tests/faults/             fault injection tests (run after integration)
tests/experiments/        experiment tests (run last)
tests/load.py             traffic generator (runs via `make load`)
scripts/smoke.py          host-side health check (`make smoke`, also the last step of `make reset`)
scripts/acceptance.py     host-side run of every scenario (`make acceptance`)
```
