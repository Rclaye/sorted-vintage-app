"""T062: POST /orders, GET /orders and GET /orders/{order_id} (FR-009, FR-013).

Checkout order of work: claim the Idempotency-Key, reserve through Listings with the buyer's
own token, store the pending order, then ask Payments for the PaymentIntent. If Payments cannot
help, the hold is released and the order fails before the answer goes back. The client secret
goes to the buyer and is never logged.
"""

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import store
from app.clients import RefusedError, UnavailableError
from app.errors import ApiError
from app.telemetry import tag_order

log = logging.getLogger(__name__)
router = APIRouter()

# Listings' refusals the buyer should see as they are (contracts/orders.openapi.yaml).
_PASS_THROUGH = {(403, "own_listing"), (409, "not_available")}


class CheckoutRequest(BaseModel):
    listing_id: uuid.UUID


def caller_sub(request: Request) -> uuid.UUID:
    try:
        return uuid.UUID(str(request.state.claims.get("sub")))
    except ValueError as exc:
        raise ApiError(403, "invalid_subject", "The token's subject is not an account id.") from exc


@router.post("/orders", status_code=201)
def create_order(
    body: CheckoutRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
) -> Response:
    app = request.app.state
    engine, settings = app.engine, app.settings
    buyer = caller_sub(request)
    key = idempotency_key.strip()
    order_id = uuid.uuid4()
    tag_order(order_id)

    earlier = store.claim_key(engine, buyer, key, order_id)
    if earlier is not None:
        tag_order(earlier.order_id)
        return _replay(engine, earlier.order_id)

    try:
        held = app.listings.reserve(body.listing_id, order_id, request.headers["authorization"])
    except RefusedError as exc:
        store.forget_key(engine, buyer, key)
        if (exc.status_code, exc.error) in _PASS_THROUGH:
            raise ApiError(exc.status_code, exc.error, exc.message or exc.error) from exc
        log.warning("Listings refused a reservation", extra={"reason": str(exc)})
        raise ApiError(exc.status_code, exc.error, "Listings refused the reservation.") from exc
    except UnavailableError as exc:
        store.forget_key(engine, buyer, key)
        log.warning("Listings unavailable for checkout", extra={"reason": str(exc)})
        raise ApiError(
            503, "listings_unavailable", "Listings cannot be reached. Try again."
        ) from exc

    fee_pct = Decimal(settings.fee_pct)
    store.insert_pending(
        engine,
        order_id=order_id,
        listing_id=body.listing_id,
        buyer_sub=buyer,
        seller_sub=held.seller_sub,
        amount_cents=held.price_cents,
        fee_pct=fee_pct,
        reservation_id=held.id,
        reservation_expires_at=held.expires_at,
    )

    try:
        intent = app.payments.create_intent(order_id, held.price_cents, fee_pct, held.seller_sub)
    except (UnavailableError, RefusedError) as exc:
        refused = isinstance(exc, RefusedError) and exc.error == "seller_not_ready"
        reason = "seller_not_ready" if refused else "payments_unavailable"
        _undo(request, order_id, held.id, reason)
        store.forget_key(engine, buyer, key)
        log.warning(
            "checkout failed at Payments",
            extra={"order_id": str(order_id), "reason": str(exc)},
        )
        if refused:
            raise ApiError(409, "seller_not_ready", "The seller cannot take payments yet.") from exc
        raise ApiError(
            503, "payments_unavailable", "Payments cannot be reached. Try again."
        ) from exc

    store.attach_intent(
        engine, order_id, buyer, key, intent.payment_intent_id, intent.client_secret
    )
    log.info(
        "checkout started",
        extra={"order_id": str(order_id), "payment_intent": intent.payment_intent_id},
    )
    order = store.as_json(store.find(engine, order_id))
    return JSONResponse({**order, "client_secret": intent.client_secret}, status_code=201)


def _undo(request: Request, order_id: uuid.UUID, reservation_id: uuid.UUID, reason: str) -> None:
    """Release the hold (best effort: Listings' sweeper ends it anyway) and fail the order."""
    app = request.app.state
    try:
        app.listings.release(reservation_id)
    except (UnavailableError, RefusedError) as exc:
        log.warning(
            "release after a failed checkout did not go through",
            extra={"order_id": str(order_id), "reason": str(exc)},
        )
    store.close(app.engine, order_id, "failed", app.settings.env, failure_reason=reason)


def _replay(engine, order_id: uuid.UUID) -> Response:
    row = store.find(engine, order_id)
    if row is None:
        raise ApiError(
            409, "checkout_in_progress", "A checkout with this Idempotency-Key is still running."
        )
    body = store.as_json(row)
    secret = store.client_secret_for(engine, order_id)
    if secret and row.status == "pending":
        body["client_secret"] = secret
    return JSONResponse(body, status_code=200)


@router.get("/orders")
def list_orders(request: Request) -> list[dict]:
    rows = store.for_buyer(request.app.state.engine, caller_sub(request))
    return [store.as_json(row) for row in rows]


@router.get("/orders/{order_id}")
def get_order(order_id: uuid.UUID, request: Request) -> dict:
    tag_order(order_id)
    row = store.find(request.app.state.engine, order_id)
    if row is None or caller_sub(request) not in (row.buyer_sub, row.seller_sub):
        raise ApiError(404, "not_found", "No such order.")
    return store.as_json(row)
