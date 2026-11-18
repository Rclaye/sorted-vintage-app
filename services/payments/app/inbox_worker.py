"""T067, T068: the inbox worker (research R8, FR-011, FR-012).

Each stored payment event is reported to Orders, and what Orders answers decides what Payments
does with the money:

  succeeded -> Orders says paid      -> the sale pair in the ledger
  succeeded -> Orders says refunded  -> a Stripe refund (transfer and fee reversed), then the
               or failed                sale pair and its negative pair: the ledger sums to zero
  failed    -> Orders says failed    -> the PaymentIntent is cancelled, so a second try on the
                                        same page cannot pay for an order that is already over
  canceled  -> Orders says failed    -> nothing more to do

Every step is idempotent (Orders ignores repeats, Stripe has idempotency keys, the ledger is
unique per order), so an event is simply retried, with backoff, until `applied_at` is set.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, or_, select, update

from app import ledger
from app.charges import find_by_intent
from app.db import payment_event, payment_intent
from app.orders_client import Orders, OrdersUnavailableError
from app.stripe_api import ProviderRejectedError, ProviderUnavailableError, StripeCharges
from app.telemetry import order_scope, tag_order

log = logging.getLogger(__name__)

OUTCOMES = {
    "payment_intent.succeeded": "succeeded",
    "payment_intent.payment_failed": "failed",
    "payment_intent.canceled": "canceled",
}
SOURCES = ("webhook", "reconciliation")
BATCH = 50
BACKOFF_FIRST_SECONDS = 5
BACKOFF_MAX_SECONDS = 300


class NotYetError(Exception):
    """The event cannot be finished now; it stays in the inbox for the next try."""


def due(engine: Engine, limit: int = BATCH) -> list:
    now = datetime.now(UTC)
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(payment_event)
                .where(
                    payment_event.c.applied_at.is_(None),
                    payment_event.c.source.in_(SOURCES),
                    payment_event.c.type.in_(OUTCOMES),
                    or_(
                        payment_event.c.next_attempt_at.is_(None),
                        payment_event.c.next_attempt_at <= now,
                    ),
                )
                .order_by(payment_event.c.received_at)
                .limit(limit)
            )
        )


def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_FIRST_SECONDS * 2 ** min(attempts, 10), BACKOFF_MAX_SECONDS)


def _mark(engine: Engine, event_id: str, **values) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(payment_event)
            .where(payment_event.c.stripe_event_id == event_id)
            .values(**values)
        )


def _settle(conn, order_id, status: str) -> None:
    conn.execute(
        update(payment_intent)
        .where(payment_intent.c.order_id == order_id)
        .values(order_status=status, settled_at=datetime.now(UTC))
    )


def _failure_reason(event) -> str | None:
    try:
        return event.payload["data"]["object"]["last_payment_error"]["code"]
    except (KeyError, TypeError):
        return None


def apply(engine: Engine, orders: Orders, charges: StripeCharges, event) -> str:
    """Applies one event end to end; returns what was done. Raises to be tried again."""
    outcome = OUTCOMES[event.type]
    known = find_by_intent(engine, event.payment_intent_id or "")
    if known is None:
        # Not created through POST /payment-intents (a spike, the Dashboard): nothing to tell.
        return "not_ours"
    tag_order(known.order_id)
    context = {"order_id": str(known.order_id), "event_id": event.stripe_event_id}

    try:
        status = orders.payment_result(
            known.order_id,
            known.payment_intent_id,
            outcome,
            event.stripe_event_id,
            _failure_reason(event) if outcome == "failed" else None,
        )
    except OrdersUnavailableError as exc:
        raise NotYetError(f"Orders: {exc}") from exc

    money = {
        "order_id": known.order_id,
        "seller_sub": known.seller_sub,
        "amount_cents": known.amount_cents,
        "fee_pct": known.fee_pct,
    }
    if outcome == "succeeded":
        if status == "paid":
            with engine.begin() as conn:
                ledger.write(
                    conn,
                    known.order_id,
                    known.seller_sub,
                    "sale",
                    ledger.sale_entries(known.amount_cents, known.fee_pct),
                )
                _settle(conn, known.order_id, "paid")
            return "sale_recorded"
        if status in ("refunded", "failed"):
            if status == "failed":
                log.warning(
                    "payment succeeded for an order that had failed; refunding", extra=context
                )
            try:
                charges.refund(known.payment_intent_id, str(known.order_id))
            except (ProviderUnavailableError, ProviderRejectedError) as exc:
                raise NotYetError(f"refund: {exc}") from exc
            ledger.record_refund(engine, **money)
            with engine.begin() as conn:
                _settle(conn, known.order_id, "refunded")
            return "refunded"
        raise NotYetError(f"Orders left the order {status} after a success")

    # failed or canceled
    if status == "failed":
        if outcome == "failed":
            try:
                charges.cancel_intent(known.payment_intent_id)
            except (ProviderUnavailableError, ProviderRejectedError) as exc:
                # A later success on this PaymentIntent is still caught above and refunded.
                log.warning(
                    "cancel after a failed payment did not go through",
                    extra={**context, "reason": str(exc)},
                )
        with engine.begin() as conn:
            _settle(conn, known.order_id, "failed")
        return "failed"
    if status in ("paid", "refunded"):
        return "stale"  # an older event for an order that has since been settled
    raise NotYetError(f"Orders left the order {status} after {outcome}")


def run_once(engine: Engine, orders: Orders, charges: StripeCharges) -> int:
    """One pass over the events that are due; returns how many were applied."""
    applied = 0
    for event in due(engine):
        # Untagged until apply finds the order; nothing carries over to the next event (T083).
        with order_scope():
            applied += _apply_one(engine, orders, charges, event)
    return applied


def _apply_one(engine: Engine, orders: Orders, charges: StripeCharges, event) -> int:
    """Applies one event and records the outcome; 1 if applied, 0 if it is retried later."""
    context = {"event_id": event.stripe_event_id, "type": event.type}
    try:
        done = apply(engine, orders, charges, event)
    except NotYetError as exc:
        wait = backoff_seconds(event.attempts)
        _mark(
            engine,
            event.stripe_event_id,
            attempts=event.attempts + 1,
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=wait),
        )
        log.warning(
            "payment event not applied yet",
            extra={**context, "reason": str(exc), "retry_in": wait},
        )
        return 0
    _mark(engine, event.stripe_event_id, applied_at=datetime.now(UTC))
    log.info("payment event applied", extra={**context, "result": done})
    return 1


async def run_forever(
    engine: Engine, orders: Orders, charges: StripeCharges, interval_seconds: float
) -> None:
    while True:
        try:
            await asyncio.to_thread(run_once, engine, orders, charges)
        except Exception:
            log.exception("inbox worker pass failed")
        await asyncio.sleep(interval_seconds)
