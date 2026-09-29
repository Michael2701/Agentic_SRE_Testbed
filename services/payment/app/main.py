import uuid

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="payment")


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
async def create_payment(body: PaymentRequest) -> PaymentResponse:
    return PaymentResponse(
        payment_id=str(uuid.uuid4()),
        status="approved",
        order_id=body.order_id,
        amount_cents=body.amount_cents,
        currency=body.currency,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "payment"}
