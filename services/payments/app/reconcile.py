"""T045: onboarding reconciliation at startup and every 5 minutes (research R5).

Events are only forwarded while stripe listen runs, so a seller who finishes onboarding while
the lab is off would otherwise stay pending. The loop asks Stripe about every account that is
not ready and applies the answer through the same code as the Connect webhook.
"""

import asyncio
import logging

from sqlalchemy import Engine

from app import accounts
from app.stripe_api import StripeConnect

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


async def run_forever(engine: Engine, connect: StripeConnect, interval_seconds: int) -> None:
    """Startup pass, then one every interval, until the app stops."""
    while True:
        try:
            await asyncio.to_thread(reconcile_accounts, engine, connect)
        except Exception:
            log.exception("onboarding reconciliation failed; trying again next interval")
        await asyncio.sleep(interval_seconds)
