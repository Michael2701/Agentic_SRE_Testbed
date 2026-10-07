# Decision log

Newest last. Format: milestone: decision (reason).

- M1: opaque tokens in Redis instead of JWT (the simplest fake auth, and it puts a real dependency on the
  auth path to break later).
- M1: no shared library between services (keeps them independent for per-service faults).
- M1: gateway `/ready` checks downstream `/ready` (one call proves auth+Redis and order+Postgres).
- M1: integration tests run in a container through nginx (real public path, no host deps).
- M1: failed payment keeps the order as `payment_failed` + 502 (state stays consistent, no lost rows).
- M2: shared `libs/observability` for telemetry only (user-approved; avoids 4× drift, M3 OTel builds on it).
- M2: pure ASGI middleware, not BaseHTTPMiddleware (clean contextvar propagation, sees the real status).
- M2: service identity via Prometheus `job` label rather than a `service` metric label.
- M2: request_id kept out of Loki labels (cardinality); line filter `|= id` instead.
- M2: Grafana Alloy instead of Promtail (Promtail is EOL).
- M2: cAdvisor dropped (incompatible with Docker Desktop's containerd store); revisit in M5.
- M2: nginx upstream re-resolves via Docker DNS (container recreation must not break routing).
- M2: an early, minimal `make load` (M9 lists it) because "observe normal behaviour" needs traffic.
- M3: Tempo as the trace backend, with services exporting to it directly (no OTel Collector; fewer moving parts).
- M3: Python exports OTLP/HTTP (lighter deps than gRPC and fewer PyPI failures); nginx uses gRPC (module limit).
- M3: nginx is the trace root via `nginx:*-alpine-otel`, so traces start where the client enters.
- M3: 100% sampling (testbed, low volume; every experiment request must be traceable).
- M3: request_id kept alongside trace_id (request_id works without tracing, e.g. when Tempo itself is faulted).
- M3: dashboards generated from `grafana/generate_dashboards.py`, now committed (source of truth).
- M4: control plane invisible to the diagnostic plane from day one (no injector telemetry; `/__*` excluded;
  Alloy drops injector logs). This prepares the M7 CONTROL ≠ DIAGNOSTIC split.
- M4: service_unavailable = real `docker stop` via the Docker API (honest connection-refused), not an app flag.
- M4: payment latency/error = app-level hook, because the simulator *is* the dependency. The hook stays
  payment-only until M5 needs a second one.
- M4: SQLite for fault state (not the app Redis, which is a future fault target; survives injector restarts).
- M4: desired-state reconcile loop (2s) instead of one-shot apply (faults survive target restarts).
- M4: Docker API via httpx over the unix socket (no docker SDK dependency).
- M5: real mechanisms everywhere except the app-level faultpoint: exec'd hogs under real cgroup limits,
  a PG trigger with pg_sleep, real held connections and table locks, Redis CLIENT PAUSE, docker pause.
- M5: `libs/faultpoint` is a separate lib from `libs/observability` (control plane ≠ telemetry).
- M5: container metrics from each service's own cgroup files instead of cAdvisor (works on Docker Desktop).
- M5: app resource limits (0.5 CPU / 256 MiB); without quotas, CPU/memory faults are meaningless.
- M5: the app DB role is a non-superuser + `max_connections=40`, so exhaustion is deterministic while
  superuser slots stay usable.
- M5: schema/roles via idempotent migrations (db-migrate service) instead of initdb (works on existing volumes).
- M5: `redis_latency` via CLIENT PAUSE, because DEBUG SLEEP is disabled in Redis 7 (`enable-debug-command no`).
  Network-level latency (tc/netem) is left for M6.
- M6: network faults via a throwaway helper container in the target's netns (Pumba-style), built from the
  injector image, instead of adding NET_ADMIN + iproute2 to app images or a permanent sidecar per service.
- M6: config faults are real redeploys (container recreated through the Docker API with changed env) rather
  than runtime config endpoints: the process restarts and reads the config, like a real rollout. No compose
  CLI inside the injector; the baseline is saved in SQLite and restored.
- M6: one netns fault per target and one of stop/pause/redeploy per container (409) instead of merging.
- M6: bad_deployment crash = broken entrypoint + restart on-failure (a crash loop), so its logs are shipped.
- M6: `SERVICE_VERSION` in telemetry (`app_build_info`, log `version`, `service.version`): without it a
  bad deployment can't be diagnosed.
- M7: experiment-runner is its own service (user's choice) instead of living in the fault-injector: the
  injector stays a fault API; the runner orchestrates through it and nginx like an external harness.
- M7: the runner generates its own traffic (same mix/User-Agent as `load.py`, code duplicated because the
  images have separate build contexts) so it can attribute every request to a phase.
- M7: verdict from client-side stats (exact per phase); a Prometheus snapshot is stored alongside for
  comparison with what the diagnostic plane saw.
- M7: the record splits `ground_truth` (hidden) from `incident` (window + symptoms) already now, so future
  investigators can be given only the incident.
- M8: same-symptom threshold is absolute (p95 > 2 s, 201) with identical traffic (user's Q2).
- M8: pool exhaustion = small pool + slightly slow DB (Q1: B), not a new injector mode.
- M8: bounded client concurrency (`max_in_flight: 10`) for the same-symptom set (Q4: B), because open-loop
  load makes saturation causes flip between "fine" and "collapsed" run to run.
- M8: proxy config fault via include snippets on a shared volume + graceful reload (a config-management
  rollout), not by editing the bind-mounted nginx.conf.
- M8: the runner's per-domain view deliberately excludes the proxy: it models what the dashboards cover,
  which is what model-breaking scenarios exploit.
- M8: order pool 30 and httpx keepalive 100 as the baseline (realistic sizing; see challenges.md).
- M9: `make reset` = recover + `down -v` + up + smoke (not a pytest test: it would tear down its own container).
- M9: acceptance is a host script over the runner API, not pytest: long, sequential, a table for humans.
- M9: domain latency margin +200 ms (symptom stays +50 ms): bursts lift unrelated edges by ~50 ms.
- M9: slow-cpu = `cpu_limit` + runner-side `baseline_relative` calibration (user's choice over relaxing the
  criteria or dropping the scenario): fixed CPU fault sizes are neither reproducible nor portable.
- M9: nginx config directory mount + `-c` instead of a file mount (inode gotcha, see stage0.md).
- M6: incorrect_timeout default 5ms: 20ms produced no errors at 20 rps (latent), 5ms clips the tail (~6% 504).
- M10 (review hardening, full-project code review): one PR for all findings the user approved (52 items);
  per-item outcomes in [review.md](review.md).
- M10: nested timeouts by raising the gateway to 8s, not by lowering order→payment below 5s: the calibrated
  payment scenarios (2.5s) keep their margin. Side effect: connection_failure drop is now a slow 502 from
  order (it records `payment_failed`) instead of a 504 from the gateway.
- M10: DB/Redis timeouts (10s / 5s) sit above every calibrated slowdown, so they only bound real hangs and
  don't change any scenario's symptoms.
- M10: onset scan stays at observe start (not inject start): a decoy rollout's restart blip would become the
  onset of deploy-then-payment.
- M10: enforce reports errors per fault instead of failing the pass; a failed rollback keeps the fault
  `failed` + `needs_cleanup` (retried by DELETE) instead of a new state, so the SQLite CHECK stays as is.
- Portal (after M10, user's request): one address `localhost:8000` with paths, not `*.localhost` subdomains
  (not cross-platform: the user's requirement) and not routes on the app's nginx (would put control-plane
  traffic into diagnostic logs/traces). Grafana/Prometheus prefix via root_url / external-url with the portal
  stripping it, not serve-from-sub-path / route-prefix, so nothing in the network changes.
- Control Center (user: "links to five systems isn't a portal"): one SPA in the portal instead of a page of
  links. No build step (Preact + htm, uPlot vendored) so the repo needs no Node toolchain and works offline;
  data straight from the existing APIs (Prometheus, Loki/Tempo via Grafana proxy, injector, runner) — the only
  backend addition is `GET /fault-types` so forms follow the catalog instead of duplicating it.
