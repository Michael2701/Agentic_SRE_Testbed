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
