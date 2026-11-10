"""T043: Stripe-hosted onboarding and payout status (FR-014, research R17)."""

import logging
import uuid

from fastapi import APIRouter, Request

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
