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
- M6: incorrect_timeout default 5ms: 20ms produced no errors at 20 rps (latent), 5ms clips the tail (~6% 504).
