"""T045, T069: reconciliation at startup and every 5 minutes (research R5).

T069, payments: a PaymentIntent whose order is still open after 2 minutes is read from Stripe,
and its outcome is stored in the inbox as `recon:<payment_intent_id>:<status>`, so it goes
through the same worker as a webhook event and a payment that finished while the forwarder was
off still settles.

T045, onboarding:

Events are only forwarded while stripe listen runs, so a seller who finishes onboarding while
the lab is off would otherwise stay pending. The loop asks Stripe about every account that is
not ready and applies the answer through the same code as the Connect webhook.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select

from app import accounts
from app.db import payment_intent
from app.stripe_api import StripeCharges, StripeConnect

log = logging.getLogger(__name__)


def refresh_account(engine: Engine, connect: StripeConnect, stripe_account_id: str) -> bool:
    """Reads an account's capabilities from Stripe and records them; True when now ready."""
    caps = connect.capabilities(stripe_account_id)
    if not accounts.apply_capabilities(engine, stripe_account_id, caps):
        log.info(
            "event for an account Payments does not know",
            extra={"stripe_account": stripe_account_id},
        )
        return True  # nothing of ours to wait for
    return accounts.payout_status(caps.card_payments, caps.stripe_transfers) == "ready"


def reconcile_accounts(engine: Engine, connect: StripeConnect) -> int:
    """One pass over the accounts that are not ready; returns how many were refreshed."""
    refreshed = 0
    for account_id in accounts.not_ready(engine):
        try:
            refresh_account(engine, connect, account_id)
            refreshed += 1
        except Exception:
            log.warning(
                "could not refresh a connected account",
                extra={"stripe_account": account_id},
                exc_info=True,
            )
    if refreshed:
        log.info("onboarding reconciliation refreshed accounts", extra={"count": refreshed})
    return refreshed


# What a PaymentIntent's status means as an event; other statuses are still in progress.
_EVENT_FOR = {"succeeded": "payment_intent.succeeded", "canceled": "payment_intent.canceled"}
# Older than this and still open: stop asking (an abandoned checkout Orders never cancelled).
_GIVE_UP_AFTER = timedelta(days=7)


def reconcile_payments(engine: Engine, charges: StripeCharges, pending_after_seconds: float) -> int:
    """One pass over the open PaymentIntents; returns how many outcomes were stored."""
    from app.webhooks import store  # webhooks imports this module

    now = datetime.now(UTC)
    with engine.connect() as conn:
        open_intents = list(
            conn.execute(
                select(payment_intent.c.payment_intent_id).where(
                    payment_intent.c.settled_at.is_(None),
                    payment_intent.c.created_at < now - timedelta(seconds=pending_after_seconds),
                    payment_intent.c.created_at > now - _GIVE_UP_AFTER,
                )
            ).scalars()
        )
    stored = 0
    for payment_intent_id in open_intents:
        try:
            intent = charges.retrieve_intent(payment_intent_id)
        except Exception:
            log.warning(
                "could not read a PaymentIntent",
                extra={"payment_intent": payment_intent_id},
                exc_info=True,
            )
            continue
        status, event_type = intent.status, _EVENT_FOR.get(intent.status)
        if (
            event_type is None
            and intent.status == "requires_payment_method"
            and intent.failure_code
        ):
            status, event_type = "payment_failed", "payment_intent.payment_failed"
        if event_type is None:
            continue
        key = f"recon:{payment_intent_id}:{status}"
        payload = {
            "id": key,
            "type": event_type,
            "data": {
                "object": {
                    "id": payment_intent_id,
                    "status": intent.status,
                    "last_payment_error": {"code": intent.failure_code},
                }
            },
        }
        if store(engine, key, event_type, "reconciliation", payload, payment_intent_id):
            stored += 1
    if stored:
        log.info("payment reconciliation stored outcomes", extra={"count": stored})
    return stored


async def run_forever(
    engine: Engine,
    connect: StripeConnect,
    interval_seconds: int,
    charges: StripeCharges | None = None,
    pending_after_seconds: float = 120.0,
) -> None:
    """Startup pass, then one every interval, until the app stops."""
    while True:
        try:
            await asyncio.to_thread(reconcile_accounts, engine, connect)
        except Exception:
            log.exception("onboarding reconciliation failed; trying again next interval")
        if charges is not None:
            try:
                await asyncio.to_thread(reconcile_payments, engine, charges, pending_after_seconds)
            except Exception:
                log.exception("payment reconciliation failed; trying again next interval")
        await asyncio.sleep(interval_seconds)
