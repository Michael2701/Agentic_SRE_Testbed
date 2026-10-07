import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import faultpoint
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from observability import annotate_span, instrument
from observability.http import instrumented_client
from prometheus_client import Counter
from pydantic import BaseModel, Field

from app import db
from app.config import settings

logger = logging.getLogger("order")
ORDERS = Counter("orders_total", "Orders by final status", ["status"])
INT4_MAX = 2_147_483_647  # the columns are INTEGER


class CreateOrderRequest(BaseModel):
    item: str = Field(min_length=1)
    quantity: int = Field(gt=0, le=INT4_MAX)
    amount_cents: int = Field(gt=0, le=INT4_MAX)
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
        settings.database_url, settings.db_pool_min_size, settings.db_pool_max_size, settings.db_timeout_seconds
    )
    app.state.http = instrumented_client(settings.payment_url, "payment", settings.http_timeout_seconds)
    yield
    await app.state.http.aclose()
    await app.state.pool.close()


app = FastAPI(title="order", lifespan=lifespan)
faultpoint.install(app)  # before instrument(): see faultpoint docstring
instrument(app, "order")


@app.exception_handler(TimeoutError)
async def database_timeout(request, exc: TimeoutError) -> JSONResponse:
    """No pooled connection or no query result within DB_TIMEOUT_SECONDS."""
    logger.error("database_timeout", extra={"error": repr(exc)})
    return JSONResponse(status_code=503, content={"detail": "database timed out"})


class PaymentError(Exception):
    """The payment call failed: transport error, non-2xx status or an unusable answer."""


async def charge(order: Order) -> str:
    """Calls the Payment Simulator. Returns payment_id or raises PaymentError."""
    try:
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
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise PaymentError(repr(exc)) from exc


async def record_status(order: Order, status: str, payment_id: str | None = None) -> Order | None:
    """Stores the outcome; None (logged with everything needed to fix it by hand) when the database fails."""
    try:
        return Order(**dict(await db.update_order_status(app.state.pool, order.id, status, payment_id)))
    except Exception as exc:
        logger.error("order_status_not_recorded", extra={"order_id": str(order.id), "status": status,
                                                         "payment_id": payment_id, "error": repr(exc)})
        return None


@app.post("/orders", status_code=201, response_model=Order)
async def create_order(body: CreateOrderRequest, x_user_id: str = Header()):
    order = Order(**dict(await db.insert_pending_order(
        app.state.pool, x_user_id, body.item, body.quantity, body.amount_cents, body.currency.upper()
    )))
    annotate_span({"user.id": x_user_id, "order.id": str(order.id)})

    try:
        payment_id = await charge(order)
    except PaymentError as exc:
        ORDERS.labels("payment_failed").inc()
        annotate_span({"order.status": "payment_failed"})
        logger.warning(
            "order_payment_failed",
            extra={"order_id": str(order.id), "user_id": x_user_id, "error": str(exc)},
        )
        failed = await record_status(order, "payment_failed") or order
        return JSONResponse(
            status_code=502,
            content={"detail": f"payment failed: {exc}", "order": failed.model_dump(mode="json")},
        )

    paid = await record_status(order, "paid", payment_id)
    if paid is None:  # charged, but the order still says pending: don't let the client retry blindly
        return JSONResponse(status_code=503, content={"detail": "payment succeeded but the order was not updated",
                                                      "payment_id": payment_id})
    ORDERS.labels("paid").inc()
    annotate_span({"order.status": paid.status, "payment.id": payment_id})
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
