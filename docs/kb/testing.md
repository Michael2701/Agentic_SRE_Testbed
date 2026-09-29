# Testing & manual checks

- `make test` = `docker compose --profile test run --rm --build tests`. Tests run **in a container**
  against `BASE_URL=http://nginx`, which exercises the real public path; the host needs no Python deps.
- **`tests/conftest.py` (shared by `integration/` and `faults/`)**: a session `client` fixture waits for
  `/ready` (`READY_TIMEOUT_SECONDS`, default 60), and the `token` fixture logs in as alice. It also holds
  URLs, `DEMO_USER`, `ORDER`, `eventually()`, `metric_value()`, `loki_streams()`. Import them with
  `from conftest import ...`. Keep a single conftest: two `conftest.py` files would clash on that import.
- Order: `pytest integration faults` (tests/Dockerfile CMD). Fault tests run last because they mutate state.
- `tests/faults/test_faults.py` (18 tests, ~30s): API lifecycle/validation/409/delete-all; incidents
  for latency (elapsed ≥1.5s + order dependency histogram), error (502 + `orders_total{payment_failed}`
  + warning/error logs in Loki for the request_id), and unavailable for payment/auth/order (502,
  health unreachable, recovery); reconcile re-push; control-plane isolation. The autouse
  `recover_after` fixture runs `DELETE /faults` and waits until payment `/health` and gateway `/ready` are OK.
- Not automated (the tests container has no docker socket): fault persistence across an injector
  restart. Verified manually in M4 (`docker compose restart fault-injector` → fault still active).
- `integration/test_happy_path.py` (6 tests): health/ready, login ok/bad, create order → read it back, orders
  without a token or with an invalid token → 401.
- `test_observability.py` (10 tests): request ID generated/preserved, `/metrics` per service
  (hits `http://<svc>:8000` directly), counter deltas, Prometheus targets == expected job set and all
  up, one request_id in Loki for nginx+gateway+auth+order+payment, Grafana datasources healthy and 3
  dashboards provisioned. Uses the `eventually()` helper, because telemetry is asynchronous (up to 45s).
  When adding a scrape job, update `EXPECTED_JOBS`.
- `test_tracing.py` (6 tests): the module fixture `order_trace` places an order, reads `X-Trace-ID` and
  polls Tempo until spans from all 5 services exist. It checks a single nginx root, the parent chain
  (gateway←nginx, auth/order←gateway, payment←order), redis/postgresql spans, business attributes,
  trace_id in Loki for all services, and that Grafana reaches Tempo and the `sre-traces` dashboard exists.
- Shared helpers live in `conftest.py`: URLs (`PROMETHEUS_URL`, `LOKI_URL`, `GRAFANA_URL`, `TEMPO_URL`),
  `DEMO_USER`, `ORDER`, `eventually()`. Import them with `from conftest import ...`. Module-scoped
  fixtures must not depend on the function-scoped `token`.
- Tests env: the in-network URLs above; the tests service waits for nginx, prometheus and grafana to be healthy.
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
