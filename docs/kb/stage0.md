# Stage 0 stabilization (M9)

The DoD → evidence table is in README ("Stage 0 status"). This file covers the M9 tooling, what it found
and the acceptance results.

## Tooling
- `make reset`: `DELETE /faults` first (best effort; restores redeploy baselines and the edge snippets while
  the injector still has them), then `compose --profile test down -v --remove-orphans` (volumes `pgdata`,
  `faultdata`, `experimentdata`, `edgeconf`; Prometheus/Loki/Tempo keep data only in their containers), then
  `make up` and `make smoke`. ~40 s. Verified with an active `bad_deployment` on order: back to 1.0.0, 0 faults,
  0 experiments, fresh DB. It can't be a pytest test: the tests container runs inside the compose project it
  would tear down.
- `make smoke` (`scripts/smoke.py`, host, stdlib): ready + login + order 201, no active faults, no
  `sre-testbed-*-prev` containers (an interrupted redeploy), all scrape targets up, Loki (Grafana datasource
  health) and Tempo (`/api/echo` via the Grafana datasource proxy: Tempo's health check isn't implemented in
  Grafana, and `/ready` answers 503 "waiting for 15s after being ready" for a long time).
- `make acceptance [FULL=1] [ROUNDS=N] [SCENARIOS="a b"]` (`scripts/acceptance.py`): every valid scenario
  through the runner, one at a time. Short runs use the test phases (baseline 10, observe 25, recovery window
  5); scenarios with scheduled faults get `time_scale 0.1` and observe stretched past the last `at_s`. PASS =
  completed + expected symptom + recovered + evidence matched (where declared). With ROUNDS > 1 the
  (observed, evidence, recovered) tuple must be identical across rounds. Exit 1 otherwise.
- `tests/faults/test_recovery_sweep.py` (23 tests, ~1 min): one case per fault type (the list mirrors
  `catalog.py`; `test_catalog_is_covered` counts 22) → inject → `DELETE /faults` → the same releases
  (`app_build_info`), orders 201 within 60 s, 5 orders at baseline latency (+150 ms), the same token TTL.

## Found and fixed by the sweep
- **nginx single-file bind mount** (`./nginx/nginx.conf:/etc/nginx/nginx.conf`): `git checkout`/`pull` (or an
  editor saving via rename) replaces the file with a new inode; the container keeps the deleted one (link
  count 0), so `nginx -s reload` → "open() /etc/nginx/nginx.conf failed" and every proxy fault fails with
  502 "nginx reload failed". Now the directory is mounted at `/etc/nginx/testbed` and nginx runs with
  `-c /etc/nginx/testbed/nginx.conf` (edge.py reloads with the same `-c`). The other configs (prometheus,
  loki, tempo, alloy) are still single-file mounts; they are read only at start, so a restart picks up changes.
- **`proxy_rate_limit burst=0`**: the catalog allows 0, nginx rejects `burst=0` ("invalid burst value"). The
  snippet omits `burst` when it is 0 (same meaning).
- **Domain latency threshold**: slow-redis flaked on evidence: database and payment client-side p95 rose
  3 → 53–57 ms (redis unpausing releases queued logins at once, order gets bursts) and crossed
  max(2×, +50 ms). Domains now need +200 ms (`DOMAIN_MIN_LATENCY_S`); every intended domain fault adds
  ≥ 300 ms. The symptom thresholds (slow = +50 ms) are unchanged.
- **slow-cpu wasn't reproducible, and no worker count fixes it.** `cpu_saturation` (hogs competing inside
  order's 0.5 CPU quota): 14 workers p95 1.8–3.0 s, 16 → 3.5–5.0 s, 15 → 3.65–5.01 s with 26% 504 in 1 of 8
  runs. Throughput swings ~2× run to run, and near the gateway's 5 s timeout the system collapses
  (metastable: the gateway gives up, order still burns CPU on abandoned requests; up to 85% 504).
- **CPU faults also aren't portable**: order costs ~6.3 ms CPU per order on this Mac; with 10 in flight,
  latency ≈ 10 × cost / quota, so a fixed quota (or worker count) means 2× latency on a 2× slower host.
- Fix (user's choice): new fault `cpu_limit` (quota lowered in place, `docker update --cpus`, no competing
  processes, `limits.py`) + runner calibration `baseline_relative: {"cpus": f}` on a fault spec: cpus =
  f × (CPU seconds per request of the target during the baseline, `increase(cpu)/increase(requests)`, a
  ratio so idle seconds at the edge of a short window don't skew it) × the scenario's rate. The computed
  quota is what the injector gets, so ground truth shows it. f = 0.65 first: 6/6 runs p95 2.44–4.08 s,
  ≤ 2% errors, quota 0.033–0.051; but in the final acceptance one round hit p95 4.92 s with 5% errors
  (observed `slow,errors` vs `slow`, verdict differed between rounds). f = 0.8 now: 4/4 runs p95
  2.2–3.0 s, 0% errors, well below the 5 s collapse. Absolute quotas tried: 0.1 → p95 0.15–0.5 s, 0.03 →
  4.2–5.0 s, 0.02 → collapse: the curve is steep, calibration must be per host.
- M8 commit: slow-cpu 14 → 16 workers (superseded by the above).

## Status (2026-10-06)
Final verification done on `feature/m9-stabilization` (uncommitted): `make reset` (smoke ok), `make test` ×2
(136 passed each, ~14 min each), `make acceptance ROUNDS=2` (below). Remaining: commit (on request) → PRs →
after the main merge: CLAUDE.md M9 ✅.

## Acceptance results
`make acceptance ROUNDS=2`, 2026-10-06, this Mac, short phases: **17 scenarios × 2 rounds, 0 failed, identical
verdicts.** p95 / error rate, round 1 → round 2:

| scenario | expect | observed | p95 s | err | evidence |
|---|---|---|---|---|---|
| bad-deployment | errors | errors | 0.02 | 41% / 36% | — |
| connection-failure | errors | errors | 0.02 | 88% / 84% | — |
| cpu-saturation | slow | slow | 0.17 / 0.13 | 0% | — |
| db-slow-query | slow | slow | 0.65 | 0% | — |
| deploy-then-payment | slow | slow | 2.53 / 2.52 | 0% | ✓ |
| network-latency | slow | slow | 0.33 | 0% | — |
| payment-error | errors | errors | 0.02 | 43% / 44% | — |
| payment-latency | slow | slow | 1.53 | 0% | — |
| redis-unavailable | errors | errors | 0.02 | 100% | — |
| slow-cpu | slow | slow | 3.54 / 2.25 | 0% | ✓ |
| slow-db-query | slow | slow | 2.23 | 0% | ✓ |
| slow-network | slow | slow | 4.21 | 0% | ✓ |
| slow-payment | slow | slow | 2.52 | 0% | ✓ |
| slow-pool-exhaustion | slow | slow,errors | 3.03 / 3.02 | 8% / 7% | ✓ |
| slow-proxy-config | slow | slow | 2.03 / 2.04 | 0% | ✓ |
| slow-proxy-network | slow | slow | 2.22 | 0% | ✓ |
| slow-redis | slow | slow | 2.48 / 2.47 | 0% | ✓ |

All recovered. slow-pool-exhaustion's 7–8% errors are stable across rounds, so its
verdict is reproducible. The previous run (f = 0.65) failed only on slow-cpu's verdict differing (see above).
