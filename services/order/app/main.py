import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from observability import instrument
from observability.http import instrumented_client
from prometheus_client import Counter
from pydantic import BaseModel, Field

from app import db
from app.config import settings

logger = logging.getLogger("order")
ORDERS = Counter("orders_total", "Orders by final status", ["status"])


class CreateOrderRequest(BaseModel):
    item: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    amount_cents: int = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class Order(BaseModel):
    id: uuid.UUID
    user_id: str
    item: str
    quantity: int
    amount_cents: int
    currency: str
    status: str
    payment_id: str | None
    created_at: datetime
    updated_at: datetime


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await db.create_pool(
        settings.database_url, settings.db_pool_min_size, settings.db_pool_max_size
    )
    app.state.http = instrumented_client(settings.payment_url, "payment", settings.http_timeout_seconds)
    yield
    await app.state.http.aclose()
    await app.state.pool.close()


app = FastAPI(title="order", lifespan=lifespan)
instrument(app, "order")


async def charge(order: Order) -> str:
    """Calls the Payment Simulator. Returns payment_id or raises httpx.HTTPError."""
    response = await app.state.http.post(
        "/payments",
        json={
            "order_id": str(order.id),
            "amount_cents": order.amount_cents,
            "currency": order.currency,
        },
    )
    response.raise_for_status()
    return response.json()["payment_id"]


@app.post("/orders", status_code=201, response_model=Order)
async def create_order(body: CreateOrderRequest, x_user_id: str = Header()):
    pool = app.state.pool
    order = Order(**dict(await db.insert_pending_order(
        pool, x_user_id, body.item, body.quantity, body.amount_cents, body.currency.upper()
    )))

    try:
        payment_id = await charge(order)
    except httpx.HTTPError as exc:
        failed = Order(**dict(await db.update_order_status(pool, order.id, "payment_failed")))
        ORDERS.labels("payment_failed").inc()
        logger.warning(
            "order_payment_failed",
            extra={"order_id": str(order.id), "user_id": x_user_id, "error": repr(exc)},
        )
        return JSONResponse(
            status_code=502,
            content={"detail": f"payment failed: {exc!r}", "order": failed.model_dump(mode="json")},
        )

    paid = Order(**dict(await db.update_order_status(pool, order.id, "paid", payment_id)))
    ORDERS.labels("paid").inc()
    logger.info(
        "order_created",
        extra={"order_id": str(paid.id), "user_id": x_user_id, "amount_cents": paid.amount_cents,
               "payment_id": payment_id},
    )
    return paid


@app.get("/orders/{order_id}", response_model=Order)
async def read_order(order_id: uuid.UUID, x_user_id: str = Header()) -> Order:
    row = await db.get_order(app.state.pool, order_id, x_user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    return Order(**dict(row))


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "order"}


@app.get("/ready")
async def ready() -> dict:
    try:
        await app.state.pool.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"postgres unavailable: {exc}")
    return {"status": "ready", "service": "order", "checks": {"postgres": "ok"}}
