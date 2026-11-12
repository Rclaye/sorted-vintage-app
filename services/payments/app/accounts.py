"""Connected account rows and the payout status derived from them (data-model.md, FR-014)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db import connected_account
from app.stripe_api import Capabilities


def payout_status(card_payments: str | None, stripe_transfers: str | None) -> str:
    """ready when both capabilities are active, pending when the account exists, else none."""
    if card_payments is None:
        return "none"
    if card_payments == "active" and stripe_transfers == "active":
        return "ready"
    return "pending"


def find(engine: Engine, account_sub: uuid.UUID):
    with engine.connect() as conn:
        return conn.execute(
            select(connected_account).where(connected_account.c.account_sub == account_sub)
        ).first()


def insert_if_missing(
    engine: Engine, account_sub: uuid.UUID, stripe_account_id: str, caps: Capabilities
) -> str:
    """Stores the account unless the seller has one already; returns the seller's account ID."""
    with engine.begin() as conn:
        conn.execute(
            insert(connected_account)
            .values(
                account_sub=account_sub,
                stripe_account_id=stripe_account_id,
                card_payments_status=caps.card_payments,
                stripe_transfers_status=caps.stripe_transfers,
                updated_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["account_sub"])
        )
        return conn.execute(
            select(connected_account.c.stripe_account_id).where(
                connected_account.c.account_sub == account_sub
            )
        ).scalar_one()


def link(
    engine: Engine, account_sub: uuid.UUID, stripe_account_id: str, caps: Capabilities
) -> bool:
    """Makes this seller the owner of an existing Stripe account (T051, the dev seed job).

    The account leaves whichever seller had it, and the seller leaves whichever account they
    had, so the same call after a rebuild or for a re-created user ends in the same state.
    True when a row had to be moved or replaced."""
    with engine.begin() as conn:
        moved = conn.execute(
            delete(connected_account).where(
                (connected_account.c.stripe_account_id == stripe_account_id)
                & (connected_account.c.account_sub != account_sub)
            )
        ).rowcount
        values = {
            "stripe_account_id": stripe_account_id,
            "card_payments_status": caps.card_payments,
            "stripe_transfers_status": caps.stripe_transfers,
            "updated_at": datetime.now(UTC),
        }
        previous = conn.execute(
            select(connected_account.c.stripe_account_id).where(
                connected_account.c.account_sub == account_sub
            )
        ).scalar_one_or_none()
        conn.execute(
            insert(connected_account)
            .values(account_sub=account_sub, **values)
            .on_conflict_do_update(index_elements=["account_sub"], set_=values)
        )
        return moved > 0 or previous not in (None, stripe_account_id)


def apply_capabilities(engine: Engine, stripe_account_id: str, caps: Capabilities) -> bool:
    """Records what Stripe reports for an account; False when Payments does not know it."""
    with engine.begin() as conn:
        result = conn.execute(
            update(connected_account)
            .where(connected_account.c.stripe_account_id == stripe_account_id)
            .values(
                card_payments_status=caps.card_payments,
                stripe_transfers_status=caps.stripe_transfers,
                updated_at=datetime.now(UTC),
            )
        )
        return result.rowcount > 0


def not_ready(engine: Engine) -> list[str]:
    """Stripe account IDs whose onboarding is not complete (the reconciler's work list)."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(connected_account.c.stripe_account_id).where(
                    (connected_account.c.card_payments_status != "active")
                    | (connected_account.c.stripe_transfers_status != "active")
                )
            ).scalars()
        )
