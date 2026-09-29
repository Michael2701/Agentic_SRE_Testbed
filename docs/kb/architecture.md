# Architecture & topology

`Client → nginx:8080 → gateway → auth (→ Redis)` and `gateway → order (→ PostgreSQL, → payment)`.
Observability services (prometheus, loki, alloy, grafana, exporters) are described in
[observability.md](observability.md).

- Compose project name `sre-testbed`; containers are `sre-testbed-<svc>-1`.
- Single network `backend`. Published ports: nginx `${NGINX_PORT:-8080}`, Grafana `${GRAFANA_PORT:-3000}`,
  Prometheus `${PROMETHEUS_PORT:-9090}`.
- App service images are built with **context = repo root** (`build: {context: ., dockerfile:
  services/<svc>/Dockerfile}`) so `libs/observability` can be copied in. Root `.dockerignore` excludes
  .git, docs, tests.
- App services listen on `:8000`. Images: `nginx:1.27-alpine`, `postgres:16-alpine`, `redis:7-alpine`,
  app services `python:3.12-slim`.
- Every container has a healthcheck; `depends_on: condition: service_healthy` follows the dependency graph.
  Python healthchecks use a shared YAML anchor `x-python-healthcheck` (urllib → `/health`).
- `make up` = `docker compose up -d --build --wait` (ready in ~20s).
- Volume `pgdata`; `db/init.sql` is mounted into `/docker-entrypoint-initdb.d`.
- `tests` service is behind compose profile `test` and does not start on `make up`.
- nginx: `/nginx-health` is answered by nginx itself; everything else is proxied to `gateway:8000`
  (connect 5s, read 30s). The upstream uses `resolver 127.0.0.11 valid=5s` + `server gateway:8000 resolve`
  (with a zone). Without it nginx cached the old gateway IP after a rebuild and returned 504.
  Keep this for anything that restarts or recreates containers (faults!).
- nginx JSON access log (`log_format json`) with `request_id`, `request_time_s`, `upstream_time_s`.
  `map $http_x_request_id $req_id` keeps a client ID, otherwise uses `$request_id`, and passes it upstream.
  A second server on `:8081` (not published) serves `/stub_status` for nginx-exporter.
