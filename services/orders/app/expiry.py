"""T064: pending orders whose 10-minute hold has run out (FR-009).

Every 30 seconds: release the hold in Listings and ask Payments to cancel the PaymentIntent,
then mark the order expired. It stays `pending` until Payments reports the outcome: `canceled`
makes it `failed`, and a payment that won the race makes it `refunded`, because consume then
finds the hold released (research R8, the late-success rule). An order that never got a
PaymentIntent cannot be paid, so it fails at once.

Every call is idempotent, so two replicas running this together do no harm.
"""

import asyncio
import logging

from sqlalchemy import Engine, select

from app import store
from app.clients import Listings, Payments, RefusedError, UnavailableError
from app.db import orders
from app.telemetry import order_scope

log = logging.getLogger(__name__)
BATCH = 50


def expire_due(engine: Engine, listings: Listings, payments: Payments, env: str) -> int:
    """One pass; returns how many orders it dealt with."""
    with engine.connect() as conn:
        due = list(
            conn.execute(
                select(orders)
                .where(
                    orders.c.status == "pending",
                    orders.c.expired_at.is_(None),
                    orders.c.reservation_expires_at < store.now(),
                )
                .order_by(orders.c.reservation_expires_at)
                .limit(BATCH)
            )
        )
    done = 0
    for row in due:
        # Each order's calls and log lines carry its id, and nothing carries over (T083).
        with order_scope(row.id):
            done += _expire(engine, listings, payments, env, row)
    return done


def _expire(engine: Engine, listings: Listings, payments: Payments, env: str, row) -> int:
    """Release, cancel, then mark; 1 if dealt with, 0 if it waits for the next pass."""
    context = {"order_id": str(row.id)}
    try:
        listings.release(row.reservation_id)
        if row.payment_intent_id:
            payments.cancel_intent(row.payment_intent_id)
    except (UnavailableError, RefusedError) as exc:
        log.warning("expiry deferred", extra={**context, "reason": str(exc)})
        return 0
    if row.payment_intent_id:
        store.mark_expired(engine, row.id)
        log.info("order expired; waiting for Payments to confirm the cancel", extra=context)
    else:
        store.close(engine, row.id, "failed", env, failure_reason="reservation_expired")
        log.info("order expired before a PaymentIntent existed", extra=context)
    return 1


async def run_forever(
    engine: Engine, listings: Listings, payments: Payments, env: str, interval: float
) -> None:
    while True:
        try:
            await asyncio.to_thread(expire_due, engine, listings, payments, env)
        except Exception:
            log.exception("expiry pass failed")
        await asyncio.sleep(interval)
