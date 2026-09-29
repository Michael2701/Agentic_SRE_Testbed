import uuid

import asyncpg

ORDER_COLUMNS = (
    "id, user_id, item, quantity, amount_cents, currency, status, payment_id, created_at, updated_at"
)


async def create_pool(dsn: str, min_size: int, max_size: int) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)


async def insert_pending_order(
    pool: asyncpg.Pool, user_id: str, item: str, quantity: int, amount_cents: int, currency: str
) -> asyncpg.Record:
    return await pool.fetchrow(
        f"""
        INSERT INTO orders (user_id, item, quantity, amount_cents, currency, status)
        VALUES ($1, $2, $3, $4, $5, 'pending')
        RETURNING {ORDER_COLUMNS}
        """,
        user_id, item, quantity, amount_cents, currency,
    )


async def update_order_status(
    pool: asyncpg.Pool, order_id: uuid.UUID, status: str, payment_id: str | None = None
) -> asyncpg.Record:
    return await pool.fetchrow(
        f"""
        UPDATE orders SET status = $2, payment_id = $3, updated_at = now()
        WHERE id = $1
        RETURNING {ORDER_COLUMNS}
        """,
        order_id, status, payment_id,
    )


async def get_order(pool: asyncpg.Pool, order_id: uuid.UUID, user_id: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        f"SELECT {ORDER_COLUMNS} FROM orders WHERE id = $1 AND user_id = $2",
        order_id, user_id,
    )
