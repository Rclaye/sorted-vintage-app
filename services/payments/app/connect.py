"""T043: Stripe-hosted onboarding and payout status (FR-014, research R17)."""

import logging
import re
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import accounts
from app.errors import ApiError
from app.stripe_api import ProviderRejectedError, ProviderUnavailableError

log = logging.getLogger(__name__)
router = APIRouter()

# Callers allowed to read any seller's payout status: Listings checks it before publishing
# (T046). Everyone else may read only their own.
SERVICE_READERS = frozenset({"listings"})


def caller_sub(request: Request) -> uuid.UUID:
    claims = request.state.claims
    try:
        return uuid.UUID(str(claims.get("sub")))
    except ValueError as exc:
        raise ApiError(403, "invalid_subject", "The token's subject is not an account id.") from exc


@router.post("/connected-accounts/me/onboarding-link")
def onboarding_link(request: Request) -> dict:
    sub = caller_sub(request)
    email = str(request.state.claims.get("email") or "").strip()
    engine, connect, settings = (
        request.app.state.engine,
        request.app.state.connect,
        request.app.state.settings,
    )
    try:
        row = accounts.find(engine, sub)
        if row is None:
            if not email:
                # Keycloak puts the account's email in the BFF's tokens (realm default scopes).
                raise ApiError(422, "email_required", "The token carries no email address.")
            account_id, caps = connect.create_account(str(sub), email)
            account_id = accounts.insert_if_missing(engine, sub, account_id, caps)
            log.info("connected account created", extra={"stripe_account": account_id})
        else:
            account_id = row.stripe_account_id
        page = f"{settings.public_base_url}/sell/onboarding"
        link = connect.onboarding_link(account_id, return_url=page, refresh_url=f"{page}?refresh=1")
    except ProviderUnavailableError as exc:
        log.warning("Stripe unavailable for onboarding", extra={"reason": str(exc)})
        raise ApiError(503, "provider_unavailable", "Stripe cannot be reached. Try again.") from exc
    except ProviderRejectedError as exc:
        log.error("Stripe refused an onboarding request", extra={"reason": str(exc)})
        raise ApiError(502, "provider_rejected", "Stripe refused the request.") from exc
    return {"url": link.url, "expires_at": link.expires_at}


@router.get("/connected-accounts/{account_sub}/payout-status")
def get_payout_status(account_sub: uuid.UUID, request: Request) -> dict:
    if (
        request.state.claims.get("azp") not in SERVICE_READERS
        and caller_sub(request) != account_sub
    ):
        raise ApiError(403, "forbidden", "Only the seller or Listings can read this status.")
    row = accounts.find(request.app.state.engine, account_sub)
    if row is None:
        return {"payout_status": "none"}
    return {
        "payout_status": accounts.payout_status(
            row.card_payments_status, row.stripe_transfers_status
        )
    }


class AccountLink(BaseModel):
    stripe_account_id: str


_ACCOUNT_ID = re.compile(r"acct_[A-Za-z0-9]{8,64}")


@router.put("/connected-accounts/{account_sub}")
def link_account(account_sub: uuid.UUID, body: AccountLink, request: Request) -> dict:
    """T051: attaches a seller to a Stripe account that already exists, for the dev seed job.

    Stripe will not let the platform accept its terms for a seller (Ticket 2.1), so demo
    sellers are onboarded once by hand and their accounts outlive a rebuild; this is how a
    re-created seller gets theirs back. Off unless ACCOUNT_LINK_AZP names a client (sv-dev
    only), and then only that client's own token may call it. The statuses stored are the ones
    Stripe reports now, never the caller's word."""
    settings = request.app.state.settings
    if not settings.account_link_azp:
        raise ApiError(404, "not_found", "Not found.")
    if request.state.claims.get("azp") != settings.account_link_azp:
        raise ApiError(403, "forbidden", "This client may not link accounts.")
    account_id = body.stripe_account_id.strip()
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ApiError(422, "invalid_account_id", "stripe_account_id must look like acct_...")
    engine, connect = request.app.state.engine, request.app.state.connect
    try:
        caps = connect.capabilities(account_id)
    except ProviderUnavailableError as exc:
        log.warning("Stripe unavailable for an account link", extra={"reason": str(exc)})
        raise ApiError(503, "provider_unavailable", "Stripe cannot be reached. Try again.") from exc
    except ProviderRejectedError as exc:
        log.warning("Stripe does not know the account to link", extra={"reason": str(exc)})
        raise ApiError(
            422, "unknown_stripe_account", "Stripe has no such account in this sandbox."
        ) from exc
    if accounts.link(engine, account_sub, account_id, caps):
        log.warning(
            "connected account moved to another seller",
            extra={"stripe_account": account_id, "sub": str(account_sub)},
        )
    else:
        log.info("connected account linked", extra={"stripe_account": account_id})
    return {"payout_status": accounts.payout_status(caps.card_payments, caps.stripe_transfers)}
