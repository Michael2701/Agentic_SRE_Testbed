# Testing & manual checks

- `make test` = `docker compose --profile test run --rm --build tests`. Tests run **in a container**
  against `BASE_URL=http://nginx`, which exercises the real public path; the host needs no Python deps.
- `tests/integration/conftest.py`: a session `client` fixture waits for `/ready` (`READY_TIMEOUT_SECONDS`,
  default 60); the `token` fixture logs in as alice. Tests import `DEMO_USER` from `conftest`.
- `test_happy_path.py` (6 tests): health/ready, login ok/bad, create order → read it back, orders
  without a token or with an invalid token → 401.
- `test_observability.py` (10 tests): request ID generated/preserved, `/metrics` per service
  (hits `http://<svc>:8000` directly), counter deltas, Prometheus targets == expected job set and all
  up, one request_id in Loki for nginx+gateway+auth+order+payment, Grafana datasources healthy and 3
  dashboards provisioned. Uses the `eventually()` helper, because telemetry is asynchronous (up to 45s).
  When adding a scrape job, update `EXPECTED_JOBS`.
- Tests env: `PROMETHEUS_URL`, `LOKI_URL`, `GRAFANA_URL` (in-network); the tests service waits for
  nginx, prometheus and grafana to be healthy.
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
