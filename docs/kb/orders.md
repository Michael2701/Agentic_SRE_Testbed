# Orders & payment

## Order service (`services/order/app/`)
- `POST /orders {item, quantity 1..2^31-1, amount_cents 1..2^31-1, currency="USD"}` with `X-User-Id`:
  INSERT `pending` → `POST payment /payments` → UPDATE `paid` + `payment_id` → 201.
  Payment failure (transport error, non-2xx, or an answer without `payment_id`): UPDATE `payment_failed` →
  502 `{detail, order}`. Paid but the UPDATE fails: 503 `{detail, payment_id}` and an error log
  `order_status_not_recorded` (order + payment id), so it can be fixed by hand; the row stays `pending`.
- **Timeout budget (M10):** order → payment 5s (`ORDER_HTTP_TIMEOUT_SECONDS`) < gateway → order 8s
  (`GATEWAY_HTTP_TIMEOUT_SECONDS`), so order always answers (and records the outcome) before the gateway
  gives up. With both at 5s, the gateway returned 504 for orders that order then still marked paid.
  Database: `DB_TIMEOUT_SECONDS`=10 bounds waiting for a pooled connection (asyncpg's `Pool.fetchrow` has no
  acquire timeout, so `db.py` acquires explicitly) and each query; a timeout → 503. 10s is above every
  calibrated slowdown (pool queueing in `slow-pool-exhaustion`, slow queries, held locks).
- `GET /orders/{id}` returns only the caller's order (`WHERE id AND user_id`); anything else → 404.
- SQL lives in `app/db.py` (asyncpg, raw SQL, `ORDER_COLUMNS` constant).

## Payment simulator (`services/payment/app/main.py`)
- Stateless. `POST /payments` returns `approved` with a new uuid `payment_id`, unless the faultpoint hook
  injects latency/5xx (see [faults.md](faults.md)).

## Schema (`db/migrations/001_schema.sql`, roles in `002_roles.sql`)
- Single table `orders(id uuid, user_id, item, quantity, amount_cents, currency char(3), status
  ∈ pending|paid|payment_failed, payment_id, created_at, updated_at)` with index on `user_id`.
- Migrations are plain idempotent SQL files run by the `db-migrate` service on every `make up` (works on an
  existing volume, no `down -v` needed). Write them re-runnable (`IF NOT EXISTS`, `DO $$ ... $$`).
- Roles: order connects as **`order_svc`** (non-superuser: SELECT/INSERT/UPDATE on orders) with
  `application_name=order` (set in `db.create_pool`). `reporting` is read-only (used by the
  connection-exhaustion fault). `app` stays superuser (exporter, migrations, injector admin).
- Under DB connection exhaustion order raises unhandled asyncpg errors → 500 (not 502).
- Auth's Redis client has `REDIS_TIMEOUT_SECONDS`=5 (socket and connect), above the calibrated 2.5s stalls.
