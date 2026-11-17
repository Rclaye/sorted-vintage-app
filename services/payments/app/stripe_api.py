"""The Stripe calls Payments makes: Connect onboarding on Accounts v2 (research R17) and, since
Ticket 3.5, destination charges, cancels and refunds (research R7, R8; Sprint 3 spike).

Everything Stripe-shaped stays here: the routes, the worker and the reconcilers see ids and
statuses only. HTTP goes through httpx, so tests answer for Stripe with respx.
"""

import logging
from dataclasses import dataclass
from typing import Any

import stripe

log = logging.getLogger(__name__)

# What a capability is called in each configuration (research R17).
_CARD_PAYMENTS = ("configuration", "merchant", "capabilities", "card_payments", "status")
_STRIPE_TRANSFERS = (
    "configuration",
    "recipient",
    "capabilities",
    "stripe_balance",
    "stripe_transfers",
    "status",
)
_INCLUDE = ["configuration.merchant", "configuration.recipient"]
KNOWN_STATUSES = {"active", "pending", "restricted", "unsupported"}


class ProviderUnavailableError(Exception):
    """Stripe could not be reached or failed on its side; the caller may retry."""


class ProviderRejectedError(Exception):
    """Stripe refused the request; retrying the same request will not help."""


@dataclass(frozen=True)
class Capabilities:
    card_payments: str
    stripe_transfers: str


@dataclass(frozen=True)
class OnboardingLink:
    url: str
    expires_at: str


def make_client(secret_key: str, timeout: float = 10.0) -> stripe.StripeClient:
    return stripe.StripeClient(
        secret_key,
        http_client=stripe.HTTPXClient(timeout=timeout, allow_sync_methods=True),
        max_network_retries=1,
    )


class StripeConnect:
    def __init__(self, client: stripe.StripeClient):
        self._client = client

    @property
    def client(self) -> stripe.StripeClient:
        return self._client

    def create_account(self, account_sub: str, contact_email: str) -> tuple[str, Capabilities]:
        """A seller's connected account with the R17 settings. Idempotent per seller.

        Stripe requires a contact email on an account with the recipient configuration
        (found in the dev sandbox, 2026-11-10)."""
        params: dict[str, Any] = {
            "contact_email": contact_email,
            "dashboard": "none",
            "identity": {"country": "us"},
            "defaults": {
                "responsibilities": {"fees_collector": "application", "losses_collector": "stripe"}
            },
            "configuration": {
                "merchant": {"capabilities": {"card_payments": {"requested": True}}},
                "recipient": {
                    "capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}
                },
            },
            "include": _INCLUDE,
            "metadata": {"account_sub": account_sub},
        }
        account = self._call(
            self._client.v2.core.accounts.create,
            params,
            {"idempotency_key": f"connected-account-{account_sub}"},
        )
        return account["id"], capabilities_of(account)

    def onboarding_link(self, account_id: str, return_url: str, refresh_url: str) -> OnboardingLink:
        link = self._call(
            self._client.v2.core.account_links.create,
            {
                "account": account_id,
                "use_case": {
                    "type": "account_onboarding",
                    # No "configurations" here: this API version refuses it (dev sandbox,
                    # 2026-11-10); the account's own configurations decide what is collected.
                    "account_onboarding": {
                        "return_url": return_url,
                        "refresh_url": refresh_url,
                        "collection_options": {"fields": "currently_due"},
                    },
                },
            },
        )
        return OnboardingLink(url=link["url"], expires_at=str(link["expires_at"]))

    def capabilities(self, account_id: str) -> Capabilities:
        account = self._call(
            lambda params: self._client.v2.core.accounts.retrieve(account_id, params),
            {"include": _INCLUDE},
        )
        return capabilities_of(account)

    @staticmethod
    def _call(method, params, options=None):
        return _call(method, params, options)


def _call(method, params, options=None):
    try:
        return method(params, options) if options else method(params)
    except (stripe.APIConnectionError, stripe.RateLimitError) as exc:
        raise ProviderUnavailableError(str(exc)) from exc
    except stripe.StripeError as exc:
        status = getattr(exc, "http_status", None) or 0
        if status >= 500:
            raise ProviderUnavailableError(str(exc)) from exc
        rejected = ProviderRejectedError(str(exc))
        rejected.code = getattr(exc, "code", None)
        raise rejected from exc


@dataclass(frozen=True)
class Intent:
    id: str
    status: str
    client_secret: str = ""
    failure_code: str | None = None


class StripeCharges:
    """Destination charges (T066) and refunds (T068), as the Sprint 3 spike ran them."""

    def __init__(self, client: stripe.StripeClient):
        self._client = client

    def create_intent(
        self, order_id: str, amount_cents: int, fee_cents: int, destination: str
    ) -> Intent:
        """The seller's account receives the charge; the fee comes back to the platform. The
        order ID is Stripe's idempotency key, so a retry returns the same PaymentIntent."""
        intent = _call(
            self._client.v1.payment_intents.create,
            {
                "amount": amount_cents,
                "currency": "usd",
                "application_fee_amount": fee_cents,
                "transfer_data": {"destination": destination},
                # No payment_method_types: this API version refuses it (T009).
                "automatic_payment_methods": {"enabled": True},
                "metadata": {"order_id": order_id},
            },
            {"idempotency_key": order_id},
        )
        return Intent(intent["id"], intent["status"], intent["client_secret"])

    def cancel_intent(self, payment_intent_id: str) -> bool:
        """True when this call cancelled it; False when it was already final (idempotent)."""
        try:
            _call(
                lambda params: self._client.v1.payment_intents.cancel(payment_intent_id, params), {}
            )
        except ProviderRejectedError as exc:
            if getattr(exc, "code", None) == "payment_intent_unexpected_state":
                return False
            raise
        return True

    def retrieve_intent(self, payment_intent_id: str) -> Intent:
        intent = _call(
            lambda params: self._client.v1.payment_intents.retrieve(payment_intent_id, params), {}
        )
        # A StripeObject is not a dict (no .get), and the field is absent until a try fails.
        try:
            code = intent["last_payment_error"]["code"]
        except (KeyError, TypeError):
            code = None
        return Intent(intent["id"], intent["status"], failure_code=code)

    def refund(self, payment_intent_id: str, order_id: str) -> None:
        """Everything back: the buyer's money, the seller's transfer and the platform's fee."""
        try:
            _call(
                self._client.v1.refunds.create,
                {
                    "payment_intent": payment_intent_id,
                    "reverse_transfer": True,
                    "refund_application_fee": True,
                    "metadata": {"order_id": order_id},
                },
                {"idempotency_key": f"refund-{order_id}"},
            )
        except ProviderRejectedError as exc:
            if getattr(exc, "code", None) != "charge_already_refunded":
                raise


def capabilities_of(account: Any) -> Capabilities:
    return Capabilities(_status(account, _CARD_PAYMENTS), _status(account, _STRIPE_TRANSFERS))


def _status(obj: Any, path: tuple[str, ...]) -> str:
    """A capability status, or 'restricted' when Stripe does not report one yet."""
    for key in path:
        try:
            obj = obj[key]
        except (KeyError, TypeError, IndexError):
            return "restricted"
        if obj is None:
            return "restricted"
    status = str(obj)
    if status not in KNOWN_STATUSES:
        log.warning("unexpected capability status from Stripe", extra={"status": status})
        return "restricted"
    return status
