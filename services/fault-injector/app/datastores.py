"""Real PostgreSQL / Redis mechanisms: triggers, held connections, table locks, client pauses.

Object and role names are neutral (a trigger called `orders_write_hook`, a `reporting` role) because
anything visible in pg_stat_activity or exporter metrics is diagnostic-plane evidence.
"""

import asyncio
import logging

import asyncpg
import redis.asyncio as redis

logger = logging.getLogger("datastores")

TRIGGER = "orders_write_hook"
APP_DB_ROLE = "order_svc"
MAX_HELD_CONNECTIONS = 200


class Postgres:
    def __init__(self, admin_dsn: str, reporting_dsn: str):
        self.admin_dsn = admin_dsn
        self.reporting_dsn = reporting_dsn
        self._admin: asyncpg.Connection | None = None

    async def admin(self) -> asyncpg.Connection:
        if self._admin is None or self._admin.is_closed():
            self._admin = await asyncpg.connect(self.admin_dsn, timeout=5)
        return self._admin

    async def aclose(self) -> None:
        if self._admin is not None and not self._admin.is_closed():
            await self._admin.close()

    # --- db_slow_query: every INSERT/UPDATE on orders really sleeps inside PostgreSQL
    async def ensure_slow_trigger(self, delay_ms: int, operations: list[str]) -> None:
        conn = await self.admin()
        if await conn.fetchval("SELECT 1 FROM pg_trigger WHERE tgname = $1", TRIGGER):
            return  # checked every reconcile pass; creating it again would take a table lock each time
        events = " OR ".join(op.upper() for op in operations)
        await conn.execute(f"""
            CREATE OR REPLACE FUNCTION {TRIGGER}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_sleep({delay_ms / 1000}); RETURN NEW; END $$;
            CREATE TRIGGER {TRIGGER} BEFORE {events} ON orders FOR EACH ROW EXECUTE FUNCTION {TRIGGER}();
        """)

    async def drop_slow_trigger(self) -> None:
        conn = await self.admin()
        await conn.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON orders; DROP FUNCTION IF EXISTS {TRIGGER}();")

    # --- db_connection_exhaustion: a non-superuser client grabs every normal slot
    async def hold_connections(self) -> None:
        held: list[asyncpg.Connection] = []
        # Own superuser connection (taken first, from the reserved slots): the shared admin one is used
        # concurrently by the reconcile loop, and asyncpg connections don't allow parallel operations.
        admin = await asyncpg.connect(self.admin_dsn, timeout=5)
        try:
            while True:
                held = [c for c in held if not c.is_closed()]
                await self._fill(held)
                # Evict the app's pooled connections so it has to reconnect into a full server.
                evicted = await admin.fetchval(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = $1", APP_DB_ROLE
                )
                if evicted:
                    await self._fill(held)  # take the slots just freed before the app reconnects
                await asyncio.sleep(1)
        finally:
            await asyncio.gather(*(c.close() for c in held), admin.close(), return_exceptions=True)

    async def _fill(self, held: list) -> None:
        while len(held) < MAX_HELD_CONNECTIONS:
            try:
                held.append(await asyncpg.connect(self.reporting_dsn, timeout=3))
            except asyncpg.TooManyConnectionsError:
                return

    # --- db_lock_contention: SHARE lock blocks INSERT/UPDATE (ROW EXCLUSIVE) while held
    async def lock_loop(self, hold_ms: int, interval_ms: int) -> None:
        conn = await asyncpg.connect(self.admin_dsn, timeout=5)
        try:
            while True:
                async with conn.transaction():
                    await conn.execute("LOCK TABLE orders IN SHARE MODE")
                    await asyncio.sleep(hold_ms / 1000)
                await asyncio.sleep((interval_ms - hold_ms) / 1000)
        finally:
            await conn.close()  # also releases the lock if cancelled mid-transaction


class Redis:
    def __init__(self, url: str):
        self.url = url

    # --- redis_latency: Redis really stops serving clients for pause_ms out of every interval_ms
    async def pause_loop(self, pause_ms: int, interval_ms: int) -> None:
        client = redis.from_url(self.url)
        try:
            while True:
                await client.execute_command("CLIENT", "PAUSE", pause_ms, "ALL")
                await asyncio.sleep(interval_ms / 1000)
        finally:
            try:
                await client.execute_command("CLIENT", "UNPAUSE")
            except redis.RedisError as exc:
                logger.warning("unpause_failed", extra={"error": repr(exc)})
            await client.aclose()
