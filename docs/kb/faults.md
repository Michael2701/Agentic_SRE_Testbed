# Fault injection (M4 foundation, M5 advanced faults, M6 infrastructure faults)

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
  `/tmp/.w-<id>-<n>.pid`, container label `io.testbed.rev`. Both `test_no_mechanism_names_in_logs`
  tests check that Loki never contains them (logs of the `tests` runner are excluded: a failed assertion
  quoting a fault id would otherwise fail every later run).
- netns helper containers have no compose labels, so Alloy never discovers them (it filters on the
  compose project label).
- Verified by `test_control_plane_invisible_to_diagnostic_plane`.
- ⚠️ If `alloy/config.alloy` changes while the stack is running, `make up` does **not** reload it (a bind
  mount; compose sees no change), and logs shipped before the Alloy restart stay in Loki. Use
  `docker compose up -d --force-recreate alloy loki` (Loki has no volume, so this is a clean slate).
- The experiment-runner (M7, [experiments.md](experiments.md)) is control plane too, under the same rules.
- `POST /faults/validate` (M7): validates a spec and fills defaults without applying it.

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
  one `asyncio.Lock` for all mutations). Target-dependent validation (peer ≠ target, allowed dependency
  and settings per target) is `catalog.check_target`.

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
| `network_latency` | apps, postgres, redis | `delay_ms`, `jitter_ms`=0, `peer`? | netns: `tc netem delay` on eth0 egress (only to `peer` if set) | `tc qdisc del root` |
| `packet_loss` | same | `loss_percent`=20, `peer`? | netns: `tc netem loss` | same |
| `connection_failure` | gateway, auth, order | `peer` (required), `mode` reject\|drop | netns: `iptables -A OUTPUT -d <peer ip> -p tcp -j REJECT --reject-with tcp-reset` / `DROP` | `iptables -F OUTPUT` |
| `incorrect_endpoint` | gateway (auth\|order), order (payment\|postgres), auth (redis) | `dependency`, `endpoint`? | redeploy with `<DEP>_URL` changed; default: host typo (`payment` → `payments`, doesn't resolve) | redeploy baseline |
| `incorrect_timeout` | gateway, order | `timeout_ms`=5 | redeploy with `HTTP_TIMEOUT_SECONDS` | same |
| `bad_configuration` | gateway, order, auth | `settings` {KEY: value}, allowlist `catalog.CONFIG_SETTINGS` | redeploy with those env vars | same |
| `bad_deployment` | auth, order, payment | `version`="1.1.0", `defect` crash\|errors\|slow, `error_rate`=0.5, `latency_ms`=800 | redeploy with `SERVICE_VERSION`; crash: entrypoint `app.main:application` + restart on-failure (crash loop); errors/slow: faultpoint config of the new release | same |

- **Mechanism modules:** `docker_api.py` (stop/pause/exec/resume), `workers.py` (exec hogs: pidfile check
  every pass, relaunch if the container restarted or OOM killed the hog), `datastores.py` (Postgres
  trigger/exhaustion/lock, Redis pause), `reconcile.py` (`enforce` idempotent; background tasks
  supervised per fault id and restarted if they die).
- **Conflicts** (`catalog.conflict_key`): same (type, target); one of stop/pause/redeploy per container
  (`container-state`); one netns fault per target (`network`: a single qdisc/iptables set).
- **faultpoint** (`libs/faultpoint`, used by auth, order, payment): pure ASGI middleware + `PUT/DELETE/GET
  /__faults`. Probes and `/__*` are exempt. **`faultpoint.install(app)` must come before
  `observability.instrument(app)`** so the service's own access log/RED metrics record injected
  latency/5xx. Multiple error faults on one service: the highest probability wins.
- Periodic params: `interval_ms` must be > `hold_ms`/`pause_ms` (validator).
- Connection budget: `max_connections=40`, 3 reserved for superusers (exporter, injector admin,
  exhaustion admin). The app uses the non-superuser `order_svc`, so exhaustion is deterministic while
  operators still get in.

## M8 additions
- `nginx` is a netns target; `proxy_bandwidth_limit` / `proxy_rate_limit` (mechanism `edge_config`,
  `app/edge.py`: snippets in the shared volume `edgeconf` + `nginx -s reload`); `bad_deployment
  defect=none` (harmless release). Details and conflicts: [challenges.md](challenges.md).
- `cpu_saturation.workers` limit 8 → 32.

## M6 mechanisms
**netns** (`network.py`): `Docker.run_helper` starts a throwaway container from the injector's **own image**
(it has `iproute2` + `iptables`) with `NetworkMode: container:<target>` and `CapAdd: NET_ADMIN`, runs the
script and removes it. The rules live in the target's namespace; app images are untouched. With `peer`:
`prio bands 4` with every priority mapped to band 1, netem on band 4 and a `u32 match ip dst <peer ip>`
filter, so only that edge is shaped. `ensure` caches `(container id, StartedAt, peer ip)` per fault: a
restart/recreate of the target (new netns) or of the peer (new IP) re-installs the rules on the next pass;
otherwise nothing runs. The cache is in memory, so after an injector restart the scripts run once more
(they start with a reset, so they are idempotent). A stopped/paused target or a stopped peer is skipped.
- Without `peer`, all egress of the target is shaped, including its answers to Prometheus scrapes and
  OTLP exports (realistic: a sick NIC affects everything).
- packet_loss mostly adds tens of ms (TCP tail-loss probes); only some requests hit an RTO/SYN retry and
  take seconds. Assert on the tail, not the median.

**redeploy** (`deploy.py`, `Docker.recreate`): the target container is replaced like a new release: stop,
rename the old one to `<name>-prev`, create the same name with the same image tag, host config and network
aliases but a changed env/cmd/restart policy, start, remove the old one. If create fails, the old container
is renamed back and restarted. Label `io.testbed.rev` = digest of the fault id; `ensure` only compares it
(cheap). A container **without** the label was created by compose, so its env/cmd/restart policy is saved
as the baseline (SQLite table `baselines`) before the first redeploy; `restore` redeploys the baseline, waits
healthy and deletes the row. Compose accepts restored containers (labels incl. config-hash are copied):
`make up` doesn't recreate them. `make up` after a rebuild does recreate them (a new image); the next pass
sees no label, takes a new baseline and redeploys the fault again.
- Apply doesn't wait for healthy (a crashing release is a valid outcome), except bad_deployment errors/slow,
  whose faultpoint config is pushed right after the new container is up.
- `crash` needs restart on-failure: Alloy discovers only running/restarting containers, so logs of a
  container that exited for good never reach Loki. With the crash loop the uvicorn error
  (`Error loading ASGI app. Attribute "application" not found`) is shipped on every attempt.
- Callers keep stale connections or DNS for a moment after a redeploy (httpx keep-alive, nginx resolver
  `valid=5s`): the first requests may fail differently. Tests poll until the expected status (`status_becomes`).
- The containerd image store (Docker Desktop) rejects the top-level `Image` digest in `/containers/create`;
  use `Config.Image` (the tag). Same for the helper image.

## Adding a fault type
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

M6 additions (verified by `tests/faults/test_infra_faults.py`):
| external symptom | root causes | distinguishing evidence |
|---|---|---|
| slow, still 201 | network_latency(order, peer=postgres) | order's `dependency_*{dependency="postgres"}` slow, payment edge normal; no trigger in the DB (unlike db_slow_query) |
| | packet_loss | tail only (p95 up, median almost flat), occasional multi-second outliers; scrape `up` may flap if no peer |
| | bad_deployment slow | `app_build_info` gets a new version series and the process uptime resets right when latency starts |
| fast 502 | connection_failure reject | `outcome="error"` (reset/refused) yet the peer is `up=1`, healthy and receives no requests (≠ service_unavailable) |
| | incorrect_endpoint | `dependency_call_failed` "Name or service not known"; the client just restarted (uptime reset); peer receives nothing |
| | bad_deployment crash | peer `up=0`, crash loop: repeated `Error loading ASGI app` in its logs, new `version` field (≠ clean stop: no logs) |
| | bad_deployment errors | 5xx at the service itself (its RED metrics) starting with a new `version` |
| 504 | connection_failure drop | ~5s, `outcome="timeout"` like dependency_timeout, but the peer is healthy and answers others |
| | incorrect_timeout | 504 after only ms (duration ≈ timeout), dependencies healthy, their server spans fast |
| 401 | bad_configuration TOKEN_TTL_SECONDS=2 | `expires_in` 2 on login; auth restarted (uptime reset); Redis fine |
| none until load/slow DB | bad_configuration DB_POOL_MAX_SIZE=1 | amplifier: with db_slow_query 50ms, p95 0.24s → 9.7s and 91% 5xx at 20 rps (pool queueing, no slower SQL) |

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

## Observed incidents, M6 (gateway `POST /orders`, 30s windows; baseline p95 24ms @5 rps, 14ms @20 rps)
| fault | load | symptom |
|---|---|---|
| network_latency order→postgres 100ms | 5 rps | p95 0.49s, 0% 5xx |
| packet_loss payment 30% | 5 rps | p95 0.61s, 0% 5xx |
| connection_failure order→payment reject | 5 rps | 100% 5xx, p95 unchanged (fast failures) |
| incorrect_endpoint order→payment | 5 rps | 100% 5xx, fast |
| incorrect_timeout gateway 20ms / 10ms / 5ms | 5 / 20 / 20 rps | 0% (latent) / 4% / 6% 504 → default 5ms |
| bad_configuration order pool=1 | 20 rps | no change alone; see amplifier row above |
| bad_deployment payment errors 0.5 | 5 rps | 46% 5xx |
| bad_deployment payment slow (800ms) | 20 rps | p95 0.99s, 0% 5xx |
All recover to baseline after removal (p95 14ms, 0% 5xx).
