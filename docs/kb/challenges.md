# Diagnostic challenge scenarios (M8)

Scenario files in `experiments/scenarios/`, run by the experiment-runner ([experiments.md](experiments.md)).
Tags: `basic` (M7 starters), `same-symptom`, `misleading-correlation`, `model-breaking`.

## Same symptom, different causes (tag `same-symptom`)
All with the same traffic (10 rps, `max_in_flight: 10`), all `expect: slow`: POST /orders **p95 > 2 s with
201s** (Q2: absolute threshold, same traffic). Each file declares `evidence` (degraded/normal domains) and
the test checks the runner's per-domain view against it.

**Why `max_in_flight` (Q4).** With open-loop traffic (a fixed 10 rps whatever happens) saturation causes
have no stable middle: the queue is either empty or grows until the gateway's 5 s timeout. Calibration:
CPU 8 workers p95 0.88 s, 10 workers 1.8 s *or* 5 s + 18% 504 on the next run, 12 workers 98% 504; pool
2 + 125 ms gave 1.77 s and 3.55 s in two runs. A client with a bounded pool (at 10 requests in flight a
tick is skipped and counted as `skipped`) self-stabilizes: latency ≈ in-flight / throughput. For the
fixed-latency causes p95 doesn't change; only their rps drops (to ~in-flight / latency). Throughput drop +
`skipped` is itself evidence of saturation on the client side.

| scenario | ground truth | evidence (domains) |
|---|---|---|
| slow-payment | payment_latency 2500 ms | payment DEGRADED |
| slow-db-query | db_slow_query 1100 ms (insert+update) | database DEGRADED |
| slow-pool-exhaustion | bad_configuration order pool 1..2 + db_slow_query 300 ms (Q1: B, a small pool with slightly slow queries, how pools really run dry) | database DEGRADED (pool wait is inside `pool.fetchrow`, so it counts as query latency) |
| slow-redis | redis_latency pause 2500 / interval 3000 | redis DEGRADED |
| slow-cpu | cpu_saturation order, 16 workers (14 gave p95 1.8–3.0 s, flaky against the 2 s threshold; 16 gives 3.7–4.0 s, 0 errors) | application DEGRADED (throttling); redis NORMAL. database/payment are *also* DEGRADED: a CPU-starved order inflates its own client-side timings of every dependency, a real diagnostic trap, so the evidence doesn't require them NORMAL |
| slow-network | network_latency order→payment 2100 ms | payment DEGRADED (client side only; payment server p95 normal) |
| slow-proxy-network | network_latency nginx→gateway 1100 ms: paid twice, nginx opens a new upstream connection per request (SYN + request) | all four NORMAL (also model-breaking) |
| slow-proxy-config | proxy_bandwidth_limit 200 B/s (nginx `limit_rate`, a missing "k") | all four NORMAL (also model-breaking) |

Calibration findings that changed the baseline system:
- order `DB_POOL_MAX_SIZE` 10 → **30** (compose): at 10 rps with 1.1 s writes the order holds ~22
  connections; a pool of 10 turned "slow DB" into pool exhaustion + 504s.
- `libs/observability` httpx transport keeps **100** idle connections (default 20): with a 2 s dependency
  the concurrency is ~20+, and the churn made every new connection pay the delay twice → 5 s timeouts.
- `proxy_rate_limit` (limit_req) is not used by the same-symptom set: under steady overload it rejects the
  excess with 503 by design. `proxy_bandwidth_limit` (limit_rate) gives pure latency.

## Model-breaking (tag `model-breaking`)
The runner's domain view (application, database, redis, payment: what the dashboards cover) is NORMAL,
yet clients are slow. nginx has no latency metrics (only `stub_status`), so the evidence is only in nginx
logs (`request_time_s` vs `upstream_time_s`) and traces (nginx span ≫ gateway span):
- proxy network: `upstream_time_s` is large too (the delay is on the nginx→gateway hop) while the gateway's
  own server duration is small;
- proxy config: `upstream_time_s` small, `request_time_s` large (the response trickles out of nginx).

## Misleading correlation (tag `misleading-correlation`)
`deploy-then-payment`: t=0 `bad_deployment order defect=none` (a new version 1.1.0: `app_build_info`,
uptime reset, but no harm), t=300 s `payment_latency 2500`. The incident onset is the real cause, 5 minutes
after the deployment. Rolling back the deployment (removing that fault) succeeds and changes nothing; the
test checks exactly that.

## Runner features added for M8
- Fault offsets `at_s` in a scenario (from the start of the inject phase; `observe_s` must exceed the latest
  offset by 10 s). `overrides.time_scale` scales offsets for tests (0.1: 5 min → 30 s).
- `incident.start` = **onset**: in the first half-overlapping `recovery_window_s` window with symptoms, the
  start of its first symptomatic request (not the window start: that was up to half a window early), not
  the injection time. `results.observe` covers onset → end; `results.observe_full` the whole phase.
- `domains`: Prometheus signals per domain for baseline and observe (`DOMAIN_SIGNALS` in
  `experiments.py`), judged NORMAL/DEGRADED (latency ≥ max(2×, +50 ms); ratio ≥ +0.2; up < 1; no data =
  NORMAL). `verdict.evidence_matched` compares with the scenario's `evidence`.
  - application: max CPU throttling of gateway/auth/order, their own 500 ratio (not 502/504 which blame
    a dependency);
  - database: order→postgres p95 (includes pool wait), `pg_up`;
  - redis: auth→redis p95, `redis_up`;
  - payment: payment server p95, order→payment p95, payment 5xx ratio.

## New faults (injector)
- `nginx` is a netns target (`network_latency`, `packet_loss`; peer e.g. gateway).
- `proxy_bandwidth_limit` (`bytes_per_second`) and `proxy_rate_limit` (`rate_rps`, `burst`; excess → 503):
  mechanism `edge_config` (`edge.py`). nginx.conf includes `/etc/nginx/runtime/*-http.conf` (http) and
  `*-location.conf` (location /) from the shared volume `edgeconf` (nginx ro, injector rw at `/edge`);
  `ensure` rewrites a snippet only when it changed and runs `nginx -s reload`; empty snippets = original.
  nginx logs its reloads (a realistic config-change trace, not ground truth).
- `bad_deployment defect=none`: a harmless release (the decoy).

## Observed runs (full durations: baseline 30 s, observe 60 s / 360 s, 10 rps, max_in_flight 10)
| scenario | p95 · errors · rps | domains DEGRADED | evidence |
|---|---|---|---|
| slow-payment | 2.53 s · 0% · 4.5 | payment | ✓ |
| slow-db-query | 2.26 s · 0% · 5.4 | database | ✓ |
| slow-pool-exhaustion | 3.01 s · 3% · 4.1 | database | ✓ |
| slow-redis | 2.49 s · 0% · 4.7 | redis | ✓ |
| slow-cpu | 3.47 s · 0% · 4.4 | application, database, payment (inflated by the starved order) | ✓ |
| slow-network | 2.12 s · 0% · 5.3 | payment (client side only) | ✓ |
| slow-proxy-network | 2.22 s · 0% · 4.1 | none (model-breaking) | ✓ |
| slow-proxy-config | 2.03 s · 0% · 4.7 | none (model-breaking) | ✓ |
| deploy-then-payment | 2.52 s · 0% · 9.7 | payment | ✓ deploy 292 s before onset |
All recovered in the first window. slow-pool-exhaustion's 3% errors are the order restart of the pool
resize (a redeploy); with the shortened test phases they reach ~7%, so the test allows < 10%. Baseline p95 ~20 ms, rps ~9.6 for every run. The rps drop under a symptom
is `max_in_flight` / latency (plus `skipped` ticks).

⚠️ Long runs on the laptop: macOS "Maintenance Sleep" froze the Docker VM mid-run (phases stretched to
~16 min, found via `pmset -g log`). Run long batches under `caffeinate -i`.
