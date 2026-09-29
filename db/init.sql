CREATE TABLE IF NOT EXISTS orders (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT        NOT NULL,
    item         TEXT        NOT NULL,
    quantity     INTEGER     NOT NULL CHECK (quantity > 0),
    amount_cents INTEGER     NOT NULL CHECK (amount_cents > 0),
    currency     CHAR(3)     NOT NULL DEFAULT 'USD',
    status       TEXT        NOT NULL CHECK (status IN ('pending', 'paid', 'payment_failed')),
    payment_id   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS orders_user_id_idx ON orders (user_id);
