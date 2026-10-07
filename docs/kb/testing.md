# Testing & manual checks

- `make test` = `docker compose --profile test run --rm --build tests`. Tests run **in a container**
  against `BASE_URL=http://nginx`, which exercises the real public path; the host needs no Python deps.
- **`tests/conftest.py` (shared by `integration/` and `faults/`)**: a session `client` fixture waits for
  `/ready` (`READY_TIMEOUT_SECONDS`, default 60), and the `token` fixture logs in as alice. It also holds
  URLs, `DEMO_USER`, `ORDER`, `eventually()` (transient HTTP/JSON errors count as "not yet"),
  `metric_value()` (labelled *and* unlabelled samples), `loki_streams()`, `loki_caught_up()` (a marker
  request must reach Loki before a "not in Loki" assertion), the `*_METRICS` URLs and the fault helpers `faults` (session injector client),
  `recover_after`, `inject()`, `place_order()`, `sample()`, `median_latency()`, `five_xx_ratio()`,
  `prom_value()`. Fault modules opt in with
  `pytestmark = pytest.mark.usefixtures("recover_after")`. Import them with
  `from conftest import ...`. Keep a single conftest: two `conftest.py` files would clash on that import.
- Order: `pytest integration faults experiments` (tests/Dockerfile CMD). Integration first; faults and
  experiments mutate state. The autouse session fixture `clean_start` removes faults left by an interrupted
  run and refuses to start (pytest exit) while an experiment is running (it would be someone else's).
- `tests/faults/test_faults.py` (18 tests, ~30s): API lifecycle/validation/409/delete-all; incidents
  for latency (elapsed ≥1.5s + order dependency histogram), error (502 + `orders_total{payment_failed}`
  + warning/error logs in Loki for the request_id), and unavailable for payment/auth/order (502,
  health unreachable, recovery); reconcile re-push; control-plane isolation. The autouse
  `recover_after` fixture runs `DELETE /faults` and waits until payment `/health` and gateway `/ready` are OK.
- `tests/faults/test_advanced_faults.py` (24 tests, ~2 min), the M5 DoD:
  - `test_slow_orders_symptom[5 causes]`: all 201, median ≥ max(2×baseline, +30 ms), recovery;
  - `test_failing_orders_symptom[5 causes]`: 5xx ratio in range (intermittent 0.1–0.9), recovery;
  - per-cause evidence tests (throttling, memory, sharelock, reporting connections, redis latency,
    timeout outcome);
  - validation, container-state conflict, no mechanism names in Loki.
  - `sample()` spaces requests 250 ms apart because periodic faults need it.
- `tests/faults/test_infra_faults.py` (23 tests, ~2.5 min), M6: network latency to one peer (client-side
  postgres latency up, payment edge flat), re-application after a target restart, packet-loss tail,
  connection failure reject (fast 502, peer healthy) / drop (502 from order after its 5s payment timeout), incorrect endpoint (DNS error, uptime
  reset, payment never reached, recovery), incorrect timeout (fast 504), short token TTL (401 after 3s),
  bad deployment errors (`app_build_info` 1.1.0, version in Loki) and crash loop (`up=0`, crash log in Loki),
  validation, conflicts, no mechanism names in Loki. `status_becomes()` polls past the first requests after a
  redeploy (stale keep-alive/DNS).
- `tests/faults/test_proxy_faults.py` (8 tests), M8: nginx→gateway latency invisible to the gateway's
  own metrics, bandwidth limit (slow 201, revert via reload), rate limit (503 + delay), harmless release,
  validation.
- `tests/experiments/test_challenges.py` (10 tests, ~10 min), M8: catalog tags; each same-symptom scenario
  with shortened phases (p95 > 2 s, < 10% errors, `evidence_matched`, recovered; model-breaking: all
  domains NORMAL + slow requests in nginx logs); misleading correlation with `time_scale 0.1`: rollback
  (removing the deploy fault) leaves orders > 2 s, onset ≈ the real cause, deploy ≥ 20 s earlier.
  `make test-challenges` runs only this file.
- Loki checks for leaked names exclude `service="tests"`: historical; since M10 Alloy drops the `tests` and
  `db-migrate` logs (pytest output names faults and scenarios).
- `tests/faults/test_combinations.py` (3 tests), M10: faults allowed together are enforceable and removable
  together (hogs + pause on one container, redeploy of a service with a faultpoint fault, network fault
  removed while the target is paused). All three failed before M10.
- `tests/experiments/test_experiments.py` (10 tests, ~1 min), M7: scenarios valid, full lifecycle on
  payment-latency with shortened phases (`overrides`): phase order/timestamps, ground truth linked to the
  injector fault (`experiment_id`, removed), incident without the cause, verdict, Prometheus snapshot;
  payment-error; one-at-a-time 409 + abort during observe removes faults; abort during inject (a
  redeploy in flight, M10) leaves no fault; 409 with an active ad-hoc fault;
  422s; runner invisible in Loki/Prometheus. The autouse `clean` fixture aborts running experiments and
  removes faults.
- `tests/faults/test_recovery_sweep.py` (23 tests, ~1 min), M9: every fault type injected and removed; the
  system returns to its pre-fault state (see stage0.md). Add a case when adding a fault type.
- `status_becomes()` lives in conftest (moved from test_infra_faults in M9).
- Host-side checks (not pytest, need the host's docker/ports): `make smoke`, `make acceptance` (stage0.md).
- `make test-faults` runs only `faults/`, `make test-experiments` only `experiments/`. Full `make test` (integration → faults → experiments): 141 tests, ~17 min (run long batches under `caffeinate -i` on a laptop).
- Not automated (the tests container has no docker socket): fault persistence across an injector
  restart. Verified manually in M4 (`docker compose restart fault-injector` → fault still active) and M6
  (redeploy not repeated: same container id and label; netem qdisc still there; recover restores the
  baseline and empties `baselines`).
- `integration/test_happy_path.py` (6 tests): health/ready, login ok/bad, create order → read it back, orders
  without a token or with an invalid token → 401.
- `integration/test_portal.py` (19 tests): every tool by path, Swagger under its prefix, prefixed Grafana/
  Prometheus links, no portal logs in Loki; Control Center served with all assets and no external URLs;
  `/fault-types` catalog. Browser flows (form inject/remove, experiment to verdict, log → trace) were checked
  with headless Chrome (playwright-core), not part of `make test`. `make smoke` checks the portal too.
- `test_observability.py` (10 tests): request ID generated/preserved, `/metrics` per service
  (hits `http://<svc>:8000` directly), counter deltas, Prometheus targets == expected job set and all
  up, one request_id in Loki for nginx+gateway+auth+order+payment, Grafana datasources healthy and 4
  dashboards provisioned (incl. `sre-domains`). Uses the `eventually()` helper, because telemetry is asynchronous (up to 45s).
  When adding a scrape job, update `EXPECTED_JOBS`.
- `test_tracing.py` (6 tests): the module fixture `order_trace` places an order, reads `X-Trace-ID` and
  polls Tempo until spans from all 5 services exist. It checks a single nginx root, the parent chain
  (gateway←nginx, auth/order←gateway, payment←order), redis/postgresql spans, business attributes,
  trace_id in Loki for all services, and that Grafana reaches Tempo and the `sre-traces` dashboard exists.
- Shared helpers live in `conftest.py`: URLs (`PROMETHEUS_URL`, `LOKI_URL`, `GRAFANA_URL`, `TEMPO_URL`),
  `DEMO_USER`, `ORDER`, `eventually()`. Import them with `from conftest import ...`. Module-scoped
  fixtures must not depend on the function-scoped `token`.
- Tests env: the in-network URLs above; the tests service waits for nginx, prometheus, grafana, fault-injector
  and experiment-runner to be healthy.
- `test_business_counters_increase` asserts an exact +1, so don't run `make load` concurrently with `make test`.
- Manual:
  ```bash
  TOKEN=$(curl -s -XPOST localhost:8080/login -H 'content-type: application/json' \
    -d '{"username":"alice","password":"alice"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
  curl -s -XPOST localhost:8080/orders -H "Authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' -d '{"item":"book","quantity":1,"amount_cents":1500}'
  ```
- Inspect: `docker compose exec -T postgres psql -U app -d orders -c '...'`,
  `docker compose exec -T redis redis-cli --scan --pattern 'token:*'`.
