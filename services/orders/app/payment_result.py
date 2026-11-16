"""T063: POST /orders/{order_id}/payment-result, from the Payments worker only (FR-011).

succeeded → consume the hold → paid; consume says reservation_released → refunded (Payments
then refunds); failed or canceled → release the hold → failed. A result for a final order is
recorded and ignored. When Listings cannot be reached the answer is 503 and Payments retries,
so no result is lost and none is applied twice.
"""

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import store
from app.clients import RefusedError, UnavailableError
from app.errors import ApiError

log = logging.getLogger(__name__)
router = APIRouter()


class PaymentResult(BaseModel):
    payment_intent_id: str
    outcome: Literal["succeeded", "failed", "canceled"]
    event_key: str
    failure_reason: str | None = None


@router.post("/orders/{order_id}/payment-result")
def payment_result(order_id: uuid.UUID, body: PaymentResult, request: Request) -> dict:
    app = request.app.state
    engine, settings = app.engine, app.settings
    if request.state.claims.get("azp") != settings.payments_azp:
        raise ApiError(403, "forbidden", "Only Payments may report a payment result.")

    row = store.find(engine, order_id)
    if row is None:
        raise ApiError(404, "not_found", "No such order.")
    before = row.status
    context = {"order_id": str(order_id), "event_key": body.event_key, "outcome": body.outcome}

    if row.status in store.FINAL:
        log.info("payment result for a final order ignored", extra=context)
    elif row.payment_intent_id and row.payment_intent_id != body.payment_intent_id:
        log.warning("payment result for another PaymentIntent ignored", extra=context)
    elif body.outcome == "succeeded":
        row = _succeeded(app, row)
    else:
        row = _failed(app, row, body.failure_reason or f"payment_{body.outcome}")

    store.record_result(
        engine, order_id, body.event_key, body.payment_intent_id, body.outcome, before, row.status
    )
    if row.status != before:
        log.info("order closed", extra={**context, "status": row.status})
    return store.as_json(row)


def _unavailable(exc: Exception, row) -> ApiError:
    log.warning(
        "Listings unavailable for a payment result",
        extra={"order_id": str(row.id), "reason": str(exc)},
    )
    return ApiError(503, "listings_unavailable", "Listings cannot be reached; retry the result.")


def _succeeded(app, row):
    try:
        app.listings.consume(row.reservation_id)
    except RefusedError as exc:
        if exc.error != "reservation_released":
            raise _unavailable(exc, row) from exc
        return store.close(
            app.engine, row.id, "refunded", app.settings.env, failure_reason="reservation_released"
        )
    except UnavailableError as exc:
        raise _unavailable(exc, row) from exc
    return store.close(app.engine, row.id, "paid", app.settings.env)


def _failed(app, row, reason: str):
    try:
        app.listings.release(row.reservation_id)
    except UnavailableError as exc:
        raise _unavailable(exc, row) from exc
    except RefusedError as exc:
        # Release is idempotent; a refusal means the hold is already gone (consumed is
        # impossible while the order is pending), so the order can still fail.
        log.warning(
            "release refused for a failed payment",
            extra={"order_id": str(row.id), "reason": str(exc)},
        )
    return store.close(app.engine, row.id, "failed", app.settings.env, failure_reason=reason)
