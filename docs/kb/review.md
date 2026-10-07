# M10 — full-project code review (2026-10-07)

Three parallel reviews (services + libs, fault-injector + runner, infra + tests + docs), key findings
re-checked by hand. Not found: SQL or shell injection, dashboard/scrape mismatches, request-context leaks.
The user approved 52 items; outcome per item below (details live in the topic files).

## Fixed
| ID | What | Where it's documented |
|---|---|---|
| F1 | `enforce` isolates errors per fault; a new fault fails only on its own step | faults.md (Error isolation) |
| F2 | hogs skip stopped/paused containers; their revert refuses a paused one | faults.md (Mechanism modules) |
| F3 | netns rules cleared for a paused target too | faults.md (M6 mechanisms) |
| F4 | wait for healthy after redeploying a service with faultpoint faults | faults.md (redeploy) |
| F5 | failed rollback → `failed` + `needs_cleanup`, retried by DELETE, blocks conflicts | faults.md (Failed apply) |
| F7 | an undone fault whose target isn't healthy in time is `removed` with a warning | faults.md (Revert) |
| F8 | `DELETE /faults` tries every fault, retries failures once, 502 with per-fault states | faults.md, README |
| F14 | nginx edge reload retried until it succeeds | faults.md (M8 additions) |
| F15 | the `-prev` leftover never counts as the service's container; >1 match is an error | faults.md |
| F16 | recreate starts the new container before removing the old one (rollback on failure) | faults.md (redeploy) |
| F17 | `superuser_reserved_connections=5` (lock contention + exhaustion together) | faults.md (budget) |
| F18 | startup sweep of helpers / `-prev`; shutdown cancels the loop under the lock | faults.md (Startup sweep) |
| F19 | `restore` errors when the label is there but the baseline isn't | faults.md (redeploy) |
| E6 | abort during inject: in-flight POST shielded; removal also by `experiment_id` | experiments.md |
| E9 | abort cancels only the experiment actually running | experiments.md |
| E10 | leftover abort retried until the injector answers | experiments.md |
| E11 | removal errors recorded; unremovable faults → `failed` | experiments.md |
| E12 | windows sorted by request start; rps from min/max | experiments.md (Record) |
| E20 | phases shorter than one window are scanned for onset | challenges.md |
| E21 | traffic awaits cancelled requests before closing the client | — |
| A1 | nested timeouts: gateway 8s > order→payment 5s | orders.md, decisions.md |
| A2 | every failure of charge + update handled (502 / 503 + `order_status_not_recorded`) | orders.md |
| A3 | DB acquire/query timeout 10s (→ 503), Redis socket/connect timeout 5s | orders.md, conventions.md |
| A4 | `quantity`/`amount_cents` ≤ 2^31-1 (422 instead of 500) | orders.md |
| A5 | login compares bytes, always (no 500 on non-ASCII, no timing difference) | — |
| A6 | `outcome` timeout for client-library timeouts, `cancelled` for cancellation | observability.md |
| A7 | gateway `/ready` via an uninstrumented client, checks in parallel, 2s | conventions.md |
| A8 | nginx keeps only well-formed `X-Request-ID`s (same pattern as the services) | observability.md, architecture.md |
| A9 | nginx `otel_trace_context inject` | tracing.md |
| A10 | order's default DSN is `order_svc` | — |
| A12 | gateway: malformed auth answer → 502 | — |
| A13 | unhandled exceptions answered by the middleware (500 with `x-request-id`) | observability.md |
| A14 | unknown HTTP methods → `OTHER` | observability.md |
| A15 | histogram buckets up to 30s | observability.md |
| A17 | tracer provider flushed at exit | — |
| I1 | Makefile reads and exports `.env` (ports for recipes and host scripts) | faults.md (Make), README |
| I2 | `payment_latency.latency_ms` default 2000; make targets fail on HTTP errors (`--fail-with-body`, pipefail) | faults.md, README |
| I3 | Grafana and Prometheus published on 127.0.0.1 only | architecture.md, README |
| I4 | Alloy drops `tests` and `db-migrate` logs | experiments.md, testing.md |
| I8 | `restart: unless-stopped` for infra, observability, control plane | architecture.md |
| I9 | healthchecks for Loki, Tempo, postgres-exporter; nginx waits for Tempo, Alloy for Loki | architecture.md |
| I10 | injector depends on db-migrate + Redis, runner on Prometheus | architecture.md |
| I11 | 48h retention in Loki, Tempo and Prometheus | observability.md, tracing.md |
| I13 | service images: deps layer first (stub lib), lib sources with `--no-deps` | architecture.md |
| I14 | `.dockerignore` (.lavish, .venv, .pytest_cache), `.env.example` (runner port, split timeouts) | — |
| T5 | `eventually()` treats transient HTTP/JSON errors as "not yet" | testing.md |
| T6 | session `clean_start`: removes leftover faults, refuses to run during an experiment | testing.md |
| T7 | sleeps → `eventually` / `loki_caught_up()` marker before negative Loki checks | testing.md |
| T12 | runner-invisibility test runs alone; challenge test checks the 202 | — |
| D1, D2 | README and kb drift (test commands/times, fault table, onset, retention, budget, healthchecks) | — |

## Not changed (with reason)
- **E13** (onset from inject start): would date `deploy-then-payment`'s onset at the decoy rollout's restart
  blip and break that challenge's calibration; kept, documented in challenges.md.
- **F4, second case** (network latency ≥ 3s on a faultpoint service blocks its config push, 3s push
  timeout): the push error is now charged to the faultpoint fault only and retried; not worth a longer
  timeout that would stall every reconcile pass under 100% packet loss.
- **A3, Redis `max_connections`**: a hard cap makes redis-py fail requests instead of waiting; left
  unbounded (timeouts already bound hangs).

## Deferred by the user (not in the approved set)
A11 (error details in responses), A16 (nginx upstream keepalive), T18 (service-graph test), idempotency
keys, auth on control APIs, non-root images / lockfiles / digest pins, role passwords in migrations,
schema CHECKs, duplication cleanups.
