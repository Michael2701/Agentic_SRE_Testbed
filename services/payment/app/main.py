import logging
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from observability import annotate_span, instrument
from prometheus_client import Counter
from pydantic import BaseModel, Field

from app import faults

logger = logging.getLogger("payment")
PAYMENTS = Counter("payments_total", "Payments processed", ["status"])

app = FastAPI(title="payment")
instrument(app, "payment")
app.include_router(faults.router)


class PaymentRequest(BaseModel):
    order_id: str
    amount_cents: int = Field(gt=0)
    currency: str = "USD"


class PaymentResponse(BaseModel):
    payment_id: str
    status: str
    order_id: str
    amount_cents: int
    currency: str


@app.post("/payments", response_model=PaymentResponse)
async def create_payment(body: PaymentRequest) -> PaymentResponse | JSONResponse:
    failure = await faults.apply()
    if failure is not None:
        PAYMENTS.labels("error").inc()
        logger.error("payment_processing_failed", extra={"order_id": body.order_id, "status": failure.status_code})
        return failure

    payment = PaymentResponse(
        payment_id=str(uuid.uuid4()),
        status="approved",
        order_id=body.order_id,
        amount_cents=body.amount_cents,
        currency=body.currency,
    )
    PAYMENTS.labels(payment.status).inc()
    annotate_span({"payment.id": payment.payment_id, "order.id": body.order_id, "payment.status": payment.status})
    logger.info(
        "payment_approved",
        extra={"payment_id": payment.payment_id, "order_id": body.order_id, "amount_cents": body.amount_cents},
    )
    return payment


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "payment"}
