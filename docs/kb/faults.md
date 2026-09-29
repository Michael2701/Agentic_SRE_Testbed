# Fault injection (M4)

## Rule: control plane ≠ diagnostic plane
The injector knows the ground truth. Diagnostic telemetry (Loki, Prometheus, Tempo) may show **only
symptoms**:
- fault-injector has no `instrument()`, no `/metrics`, no traces. Alloy **drops** its logs
  (`discovery.relabel` rule, action `drop`), and Prometheus doesn't scrape it.
- Control endpoints in services use the `/__` prefix. `libs/observability` excludes `/__*` from access
  logs, RED metrics (`is_unobserved()` in `middleware.py`) and traces (`EXCLUDED_URLS` in `tracing.py`).
- Hook code never logs, counts or annotates anything saying "fault". A symptom such as payment's
  `payment_processing_failed` error log or `payments_total{status="error"}` is fine; a real processor
  outage would look the same.
- Verified by `test_control_plane_invisible_to_diagnostic_plane`.
- ⚠️ If `alloy/config.alloy` changes while the stack is running, logs shipped before the Alloy restart stay
  in Loki. Loki has no volume, so `make down && make up` gives a clean slate.

## Service `services/fault-injector` (control plane, host `127.0.0.1:8090`)
- Built with context `./services/fault-injector`, **without** `libs/observability`. Plain JSON logs go to
  docker stdout only.
- API: `POST/GET /faults`, `GET/DELETE /faults/{id}`, `DELETE /faults`, `GET /health`. Returns
  201 created, 409 for a duplicate active (type, target), 422 for validation errors, and 502 when a
  fault can't be applied (stored `failed`) or reverted (stays `active` with `error`).
- Model: `id` `flt-<hex>`, `experiment_id` (`[A-Za-z0-9._-]{1,64}`, default `adhoc-<hex>`), `type`,
  `target`, `parameters` (validated and defaults filled), `state` active|removed|failed, `error`,
  `created_at`, `updated_at`. Removed faults stay as history.
- Files: `app/catalog.py` (types: param schemas with `extra="forbid"`, targets, mechanism),
  `app/store.py` (SQLite `/data/faults.db` on volume `faultdata`, stdlib), `app/docker_api.py`
  (Docker Engine API over the unix socket via httpx; container found by compose labels),
  `app/reconcile.py` (`enforce`, `revert`), `app/main.py` (API + reconcile loop every 2s,
  one `asyncio.Lock` for all mutations).

## Types
| type | target | params | mechanism |
|---|---|---|---|
| `payment_latency` | payment | `latency_ms` 1–30000, `jitter_ms`=0, `probability`=1 | `PUT payment:/__faults` |
| `payment_error` | payment | `status_code` 500–599 (=500), `probability`=1 | `PUT payment:/__faults` |
| `service_unavailable` | payment, auth, order | — | Docker `stop` (t=2); revert = `start` + wait healthy |

- `payment_latency` and `payment_error` can be active together; `reconcile.payment_config()` merges them.
- Payment hook: `services/payment/app/faults.py` (in-memory `FaultConfig`, `apply()` is called first in
  `POST /payments`). A restart clears it and the reconcile loop re-pushes within ~2s.
- While payment is stopped, config pushes are skipped. Revert starts the container, then pushes the
  remaining config.

## Adding a fault type (M5+)
1. Add a params model + `FaultType` to `catalog.py`.
2. Implement the mechanism in `reconcile.py` (`enforce` must be idempotent: it runs every 2s; also add
   `revert`).
3. If app-level, add a `/__faults` hook to the target service. **Generalize the payment hook into a
   shared module once a second service needs one** (it was deliberately kept payment-only in M4).
4. Add incident + recovery tests in `tests/faults/test_faults.py`.
5. Prefer real mechanisms (containers, network) over `sleep()` where practical (`project.md` M6).

## Make targets
`make fault TYPE=payment-latency PARAMS='{"latency_ms":2000}' [TARGET=...] [EXP=...]` (kebab → snake),
`make faults` (active), `make recover` (DELETE all). The host uses curl against `localhost:8090`.

## Observed incidents (5 rps load, 30s windows)
| fault | symptom |
|---|---|
| payment_latency 2000ms | gateway `POST /orders` p95 23ms → 2.4s; 0 errors |
| payment_error | 5xx ratio 0 → 64%, order→payment errors and `orders_total{payment_failed}` ~4/s |
| service_unavailable(payment) | 5xx ratio → 52%, order→payment outcome=error ~3.8/s |
All recover to baseline after removal.
