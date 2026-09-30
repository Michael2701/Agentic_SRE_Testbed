# Experiments (M7)

## Service `services/experiment-runner` (control plane, host `127.0.0.1:8091`)
- Separate service (not part of the injector): it only touches the system through the **fault-injector API**
  and the public **nginx** entry point, and reads Prometheus. Built without `libs/observability`, not scraped,
  logs dropped by Alloy (`fault-injector|experiment-runner` drop rule). Same isolation rule as in
  [faults.md](faults.md).
- Files: `app/scenarios.py` (scenario models, file loading, partial overrides), `app/traffic.py` (load +
  per-request samples, `summarize`), `app/experiments.py` (lifecycle, symptoms, recovery, Prometheus
  snapshot, `Injector` client), `app/store.py` (SQLite `/data/experiments.db` on volume `experimentdata`,
  one JSON doc per experiment, sequential ids `exp-<n>`), `app/main.py` (API, text summaries).
- API: `GET /scenarios[?format=text]` (each fault spec checked via injector `POST /faults/validate`),
  `POST /experiments {scenario, overrides?}[?wait=true&format=text]` → 202, `GET /experiments[?state=]`,
  `GET /experiments/{id}[?format=text]`, `DELETE /experiments/{id}` (abort: traffic stops, faults removed).
- Rules: one experiment at a time (409); 409 if any fault is already active (polluted baseline); 422 for an
  unknown scenario, invalid overrides or a fault spec the injector rejects. `wait=true` shields the task,
  so a dropped client connection doesn't cancel the run.
- Runner restart mid-run: at startup, `running` experiments get their faults removed (ids from
  ground truth) and become `aborted`.

## Scenarios (`experiments/scenarios/<name>.json`, mounted ro at `/scenarios`)
`{name (= file stem), description, faults: [{type, target?, parameters}], traffic: {rate}, durations:
{baseline_s, observe_s, recovery_window_s, recovery_timeout_s}, expect: slow|errors|auth_errors|none}`.
Starter set (one simple cause each): payment-latency, payment-error, db-slow-query, redis-unavailable,
network-latency (order→payment), cpu-saturation, connection-failure, bad-deployment. `faults` is a list so
M8 can combine causes; timed offsets between faults are not there yet (M8).

## Lifecycle (`Runner._run`)
`baseline` (gateway `/ready` must be 200, traffic starts, measure) → `inject` (faults created with
`experiment_id` = the experiment id) → `observe` → `record` (waits for in-flight requests of the window,
`Traffic.settle`, then stats + symptoms) → `remove` (DELETE each fault; container faults return when
healthy) → `recovery` (consecutive `recovery_window_s` windows until one has no symptoms and ≥ 50% of the
expected requests, or `recovery_timeout_s`). Traffic stops at the end. On error/abort the faults are removed
and the state is `failed`/`aborted`.

## Record
- `ground_truth.faults`: id, type, target, parameters, created_at. **Hidden**: control plane only.
- `incident`: `start` (inject), `end` (removal), `entry_point`, `symptoms`. No cause: this is what a
  future investigator gets. Tests check it doesn't mention the target or the fault id.
- `phases`: name, started_at, ended_at. `results`: client-side stats of order requests per phase (p50/p95/
  p99, error ratio incl. transport errors, 401 ratio, status counts; failed logins are background noise and
  excluded), recovery windows. `telemetry`: gateway p95 and 5xx ratio from Prometheus per phase (histogram
  quantiles interpolate within buckets: 1.5s client p95 reads ~2.4s between the 1 and 2.5 buckets).
- `verdict`: expected, observed, degraded, expected_symptom_seen, recovered, recovery_seconds.
- Symptom thresholds (vs baseline): slow = p95 ≥ max(2×, +50 ms); errors / auth_errors = ratio ≥ +5 pp.

## Traffic
Same mix and User-Agent (`shop-client/1.0`) as `tests/load.py`, so experiment load looks like ordinary load
in nginx logs. Tokens are issued once at start (a short token TTL fault wouldn't bite; relogin isn't
simulated).

## Make
`make scenarios`, `make experiment SCENARIO=<name>` (blocks, prints the summary), `make experiments`
(JSON incl. ground truth), `make test-experiments`.

## Observed runs (M7 DoD, default durations 30/30s, 5 rps, ~75s per run)
| scenario | baseline p95 | observe | verdict |
|---|---|---|---|
| payment-latency | 19 ms | p95 1.525s, 0% errors | slow ✓, recovered |
| payment-error | 19 ms | 46% errors | errors ✓, recovered |
| db-slow-query | 19 ms | p95 0.627s | slow ✓, recovered |
| redis-unavailable | 20 ms | 100% errors | errors ✓, recovered |
| network-latency | 19 ms | p95 0.324s | slow ✓, recovered |
| cpu-saturation | 20 ms | p95 0.131s | slow ✓, recovered |
| connection-failure | 24 ms | 79% errors (GET /orders doesn't reach payment) | errors ✓, recovered |
| bad-deployment | 19 ms | 41% errors | errors ✓, recovered |
| payment-latency (repeat) | 37 ms | p95 1.538s | same verdict: reproducible |
Recovery was seen in the first window every time (`recovery_seconds` 0.0: removal returns after the
revert, container faults once healthy). No faults left active afterwards.
