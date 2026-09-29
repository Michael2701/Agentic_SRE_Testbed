# Orders & payment

## Order service (`services/order/app/`)
- `POST /orders {item, quantity>0, amount_cents>0, currency="USD"}` with `X-User-Id`:
  INSERT `pending` → `POST payment /payments` → UPDATE `paid` + `payment_id` → 201.
  On payment `httpx.HTTPError`: UPDATE `payment_failed` → 502 `{detail, order}`.
- `GET /orders/{id}` returns only the caller's order (`WHERE id AND user_id`); anything else → 404.
- SQL lives in `app/db.py` (asyncpg, raw SQL, `ORDER_COLUMNS` constant).

## Payment simulator (`services/payment/app/main.py`)
- Stateless. `POST /payments` always returns `approved` with a new uuid `payment_id`.
  It has no fault hooks yet; those arrive in M4.

## Schema (`db/init.sql`)
- Single table `orders(id uuid, user_id, item, quantity, amount_cents, currency char(3), status
  ∈ pending|paid|payment_failed, payment_id, created_at, updated_at)` with index on `user_id`.
- There are no users and no migration tool. `init.sql` runs only on an empty `pgdata` volume, so
  schema changes need `docker compose down -v`.
