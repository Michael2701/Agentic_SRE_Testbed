# Orders & payment

## Order service (`services/order/app/`)
- `POST /orders {item, quantity>0, amount_cents>0, currency="USD"}` with `X-User-Id`:
  INSERT `pending` → `POST payment /payments` → UPDATE `paid` + `payment_id` → 201.
  On payment `httpx.HTTPError`: UPDATE `payment_failed` → 502 `{detail, order}`.
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
