"""T044: connected-account thin events from the forwarder (research R4, R17).

The signature is checked before anything is read or stored. Each event is stored once in the
Payment event inbox (a replay is a no-op, 200), and account events are applied by fetching the
account from Stripe, through the same code as the reconciler. A failure to apply after storing
still answers 200: the event is kept, unapplied, and the reconciler brings the account up to date.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import stripe
from fastapi import APIRouter, Request
from sqlalchemy import Engine, update
from sqlalchemy.dialects.postgresql import insert

from app.db import payment_event
from app.errors import ApiError
from app.reconcile import refresh_account

log = logging.getLogger(__name__)
router = APIRouter()

ACCOUNT_EVENTS = frozenset({"v1.account.updated", "v1.capability.updated"})
# Stripe's default: a signature older than five minutes is refused, which also bounds replays.
SIGNATURE_TOLERANCE_SECONDS = 300


@router.post("/webhooks/stripe-connect")
async def stripe_connect(request: Request) -> dict:
    payload = await request.body()
    secret = request.app.state.settings.stripe_webhook_secret
    try:
        stripe.WebhookSignature.verify_header(
            payload,
            request.headers.get("stripe-signature"),
            secret,
            tolerance=SIGNATURE_TOLERANCE_SECONDS,
        )
        event = json.loads(payload)
        event_id, event_type = str(event["id"]), str(event["type"])
    except (stripe.SignatureVerificationError, ValueError, KeyError, TypeError) as exc:
        log.warning("Connect webhook refused", extra={"reason": type(exc).__name__})
        raise ApiError(400, "invalid_signature", "The Stripe signature is not valid.") from exc

    # Database and Stripe calls are blocking, so they run off the event loop.
    return await asyncio.to_thread(
        handle, request.app.state.engine, request.app.state.connect, event_id, event_type, event
    )


def handle(engine: Engine, connect, event_id: str, event_type: str, event: dict) -> dict:
    if not store(engine, event_id, event_type, "connect_webhook", event):
        log.info("duplicate Connect event ignored", extra={"event_id": event_id})
        return {"received": True, "duplicate": True}

    account_id = related_account(event)
    if event_type in ACCOUNT_EVENTS and account_id:
        try:
            refresh_account(engine, connect, account_id)
        except Exception:
            log.exception("Connect event stored but not applied", extra={"event_id": event_id})
            return {"received": True, "applied": False}
    mark_applied(engine, event_id)
    return {"received": True}


def related_account(event: dict) -> str | None:
    """The connected account a thin event is about: its related object, or the event's context."""
    related = event.get("related_object") or {}
    if related.get("type") == "account" or str(related.get("id", "")).startswith("acct_"):
        return related.get("id")
    context = event.get("context")
    return context if isinstance(context, str) and context.startswith("acct_") else None


def store(engine: Engine, event_id: str, event_type: str, source: str, payload: dict) -> bool:
    """Inserts the event once; False when it was already in the inbox."""
    with engine.begin() as conn:
        result = conn.execute(
            insert(payment_event)
            .values(
                stripe_event_id=event_id,
                type=event_type,
                source=source,
                payload=payload,
                received_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["stripe_event_id"])
            .returning(payment_event.c.stripe_event_id)
        )
        # RETURNING, not rowcount: with ON CONFLICT DO NOTHING the driver's rowcount is not
        # reliable, and a row comes back only when this delivery was the first.
        return result.first() is not None


def mark_applied(engine: Engine, event_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(payment_event)
            .where(payment_event.c.stripe_event_id == event_id)
            .values(applied_at=datetime.now(UTC))
        )
