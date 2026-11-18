"""T066: PaymentIntents for Orders (FR-010, FR-014, research R7).

POST /payment-intents creates a destination charge on the seller's connected account with the
platform's fee, the order ID as Stripe's idempotency key. POST /payment-intents/{id}/cancel is
idempotent. Both are for Orders' client-credentials token only. The client secret goes back to
Orders and is never logged.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert

from app import accounts
from app.db import payment_intent
from app.errors import ApiError
from app.ledger import platform_fee
from app.stripe_api import ProviderRejectedError, ProviderUnavailableError
from app.telemetry import tag_order

log = logging.getLogger(__name__)
router = APIRouter()


class IntentRequest(BaseModel):
    order_id: uuid.UUID
    amount_cents: int = Field(gt=0)
    fee_pct: Decimal = Field(ge=0, le=100)
    seller_sub: uuid.UUID


def require_orders(request: Request) -> None:
    if request.state.claims.get("azp") != request.app.state.settings.orders_azp:
        raise ApiError(403, "forbidden", "Only Orders may use PaymentIntents.")


def remember(engine: Engine, body: IntentRequest, payment_intent_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(payment_intent)
            .values(
                order_id=body.order_id,
                payment_intent_id=payment_intent_id,
                seller_sub=body.seller_sub,
                amount_cents=body.amount_cents,
                fee_pct=body.fee_pct,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["order_id"])
        )


def find_by_intent(engine: Engine, payment_intent_id: str):
    with engine.connect() as conn:
        return conn.execute(
            select(payment_intent).where(payment_intent.c.payment_intent_id == payment_intent_id)
        ).first()


@router.post("/payment-intents", status_code=201)
def create_payment_intent(body: IntentRequest, request: Request) -> JSONResponse:
    require_orders(request)
    tag_order(body.order_id)
    app = request.app.state
    seller = accounts.find(app.engine, body.seller_sub)
    ready = seller is not None and (
        accounts.payout_status(seller.card_payments_status, seller.stripe_transfers_status)
        == "ready"
    )
    if not ready:
        raise ApiError(409, "seller_not_ready", "The seller cannot take payments yet.")

    fee = platform_fee(body.amount_cents, body.fee_pct)
    context = {"order_id": str(body.order_id)}
    try:
        intent = app.charges.create_intent(
            str(body.order_id), body.amount_cents, fee, seller.stripe_account_id
        )
    except ProviderUnavailableError as exc:
        log.warning("Stripe unavailable for a PaymentIntent", extra={**context, "reason": str(exc)})
        raise ApiError(503, "provider_unavailable", "Stripe cannot be reached. Try again.") from exc
    except ProviderRejectedError as exc:
        log.error("Stripe refused a PaymentIntent", extra={**context, "reason": str(exc)})
        raise ApiError(502, "provider_rejected", "Stripe refused the request.") from exc

    remember(app.engine, body, intent.id)
    log.info("PaymentIntent created", extra={**context, "payment_intent": intent.id})
    return JSONResponse(
        {
            "payment_intent_id": intent.id,
            "client_secret": intent.client_secret,
            "application_fee_cents": fee,
        },
        status_code=201,
    )


@router.post("/payment-intents/{payment_intent_id}/cancel")
def cancel_payment_intent(payment_intent_id: str, request: Request) -> dict:
    """200 whether this call cancelled it or it was already final; `canceled` says which, so a
    PaymentIntent that had already succeeded is not mistaken for a cancelled one."""
    require_orders(request)
    known = find_by_intent(request.app.state.engine, payment_intent_id)
    if known is not None:
        tag_order(known.order_id)
    context = {"payment_intent": payment_intent_id}
    try:
        canceled = request.app.state.charges.cancel_intent(payment_intent_id)
    except ProviderUnavailableError as exc:
        log.warning("Stripe unavailable for a cancel", extra={**context, "reason": str(exc)})
        raise ApiError(503, "provider_unavailable", "Stripe cannot be reached. Try again.") from exc
    except ProviderRejectedError as exc:
        log.error("Stripe refused a cancel", extra={**context, "reason": str(exc)})
        raise ApiError(502, "provider_rejected", "Stripe refused the request.") from exc
    log.info("PaymentIntent cancel", extra={**context, "canceled": canceled})
    return {"payment_intent_id": payment_intent_id, "canceled": canceled}
