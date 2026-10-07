# Architecture & topology

`Client → nginx:8080 → gateway → auth (→ Redis)` and `gateway → order (→ PostgreSQL, → payment)`.
Observability services (prometheus, loki, alloy, grafana, exporters) are described in
[observability.md](observability.md).

- Compose project name `sre-testbed`; containers are `sre-testbed-<svc>-1`.
- **Portal** (`portal/`, nginx-alpine, `127.0.0.1:${PORTAL_PORT:-8000}`): the one address for humans. `/` is
  the **Control Center**, a static single-page app (`portal/html/`, Preact + htm and uPlot vendored in
  `ui/vendor/`: no build step, works offline). Tabs: overview (Prometheus `query_range`/`query`, same signals
  and thresholds as the runner's domains), faults (form generated from injector `GET /fault-types` = pydantic
  JSON schemas + `per_target` hints), experiments (runner API; short run = acceptance's overrides), logs and
  traces (Loki and Tempo through Grafana's datasource proxy, own span waterfall), dashboards (Grafana in a
  kiosk iframe, `GF_SECURITY_ALLOW_EMBEDDING`). *Investigation mode* (localStorage) hides active faults,
  scenario names and ground truth. Every tool also by path, prefix stripped:
  `/grafana/`, `/prometheus/`, `/faults/`, `/experiments/`, `/app/` (→ the app's nginx). Paths, not
  `*.localhost` subdomains: those don't resolve everywhere (Windows resolver, CLI tools). Grafana gets
  `GF_SERVER_ROOT_URL=…/grafana/` and Prometheus `--web.external-url=…/prometheus/ --web.route-prefix=/`, so
  their UI links carry the prefix while they still serve from the root in the network (datasources, runner,
  Tempo remote-write, tests unchanged); Grafana's UI therefore works only through the portal (its API on
  :3000 still does). FastAPI services (injector, runner, gateway) take `X-Forwarded-Prefix` as `root_path`
  (`ForwardedPrefix` middleware: `path` includes it, as ASGI expects — otherwise `/faults/faults` 404s).
  Upstreams resolve per request (resolver + variables), so the portal survives recreated containers.
  Control plane: no access log, Alloy drops it; never in front of the app (the app's nginx logs are evidence).
  Editing: files under `portal/html/` are live on reload (served `no-cache`, no build); a `conf.d` change needs
  `docker compose exec portal nginx -s reload`. UI strings are English (shown to people). Browser checks:
  headless Chrome via `playwright-core` (not in `make test`).
- Single network `backend`. Published ports: nginx `${NGINX_PORT:-8080}` (all interfaces); on 127.0.0.1 only
  (M10): Grafana `${GRAFANA_PORT:-3000}` (anonymous Admin), Prometheus `${PROMETHEUS_PORT:-9090}`
  (remote-write receiver on), fault-injector `127.0.0.1:${FAULT_INJECTOR_PORT:-8090}` (control
  plane, see [faults.md](faults.md)), experiment-runner `127.0.0.1:${EXPERIMENT_RUNNER_PORT:-8091}` (control
  plane, see [experiments.md](experiments.md)). Docker socket is mounted by alloy (ro) and fault-injector (rw).
- The fault-injector may **recreate** app containers (M6 redeploy faults: same name, new id and IP; label
  `io.testbed.rev` while a fault is active; a stopped `<name>-prev` exists for a moment during the swap) and
  starts short-lived helper containers (label `io.testbed.helper`, no compose labels) in a target's network
  namespace.
- App service images are built with **context = repo root** (`build: {context: ., dockerfile:
  services/<svc>/Dockerfile}`) so `libs/observability` can be copied in. Root `.dockerignore` excludes
  .git, docs, tests. Dependencies are installed first from the libs' `pyproject.toml` (stub package), the lib
  sources on top with `--no-deps`: editing a lib rebuilds only the last layers.
- App services have limits `cpus: 0.5`, `mem_limit: 256m` (anchor `x-app-limits`, M5). The baseline
  p95 moved slightly, since CFS throttling adds jitter.
- Postgres runs with `max_connections=40`, `superuser_reserved_connections=5` (budget: faults.md). The **`db-migrate`** one-shot service (postgres image, psql)
  applies `db/migrations/*.sql` idempotently on every `make up`; `order` waits for
  `service_completed_successfully`. `make up --wait` is fine with a completed one-shot service.
- App services listen on `:8000`. Images: `nginx:1.27-alpine`, `postgres:16-alpine`, `redis:7-alpine`,
  app services `python:3.12-slim`.
- Healthchecks on every container whose image has a client (wget/python/pg_isready/redis-cli); alloy,
  nginx-exporter and redis-exporter have none (no shell/client in the image). `depends_on: condition:
  service_healthy` follows the dependency graph (nginx waits for Tempo, alloy for Loki, the injector for
  db-migrate + Redis, the runner for Prometheus). Python healthchecks use the YAML anchor `x-python-healthcheck`.
- Restart policy `unless-stopped` on infrastructure, observability and control plane (an explicit stop by a
  fault is honoured). App services have none: the injector manages theirs (crash-loop redeploys).
- `make up` = `docker compose up -d --build --wait` (ready in ~20s).
- Volume `pgdata`. The schema is no longer in initdb (see db-migrate above).
- `tests` service is behind compose profile `test` and does not start on `make up`.
- nginx: `/nginx-health` is answered by nginx itself; everything else is proxied to `gateway:8000`
  (connect 5s, read 30s). The upstream uses `resolver 127.0.0.11 valid=5s` + `server gateway:8000 resolve`
  (with a zone). Without it nginx cached the old gateway IP after a rebuild and returned 504.
  Keep this for anything that restarts or recreates containers (faults!).
- nginx JSON access log (`log_format json`) with `request_id`, `request_time_s`, `upstream_time_s`.
  `map $http_x_request_id $req_id` keeps a well-formed client ID (the services' pattern), otherwise uses
  `$request_id`, and passes it upstream.
  A second server on `:8081` (not published) serves `/stub_status` for nginx-exporter.
