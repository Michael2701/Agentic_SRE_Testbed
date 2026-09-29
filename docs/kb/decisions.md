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
