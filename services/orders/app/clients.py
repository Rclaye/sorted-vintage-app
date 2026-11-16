"""The calls Orders makes: Keycloak for its own token, Listings and Payments (research R8).

Listings' reserve gets the buyer's own token; consume, release and every Payments call get
Orders' client-credentials token. Network failures and 5xx answers become Unavailable, so a
caller can tell "refused" from "could not ask".
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

# Renew the service token this many seconds before Keycloak says it expires.
_EARLY = 30
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class UnavailableError(Exception):
    """The other service could not be reached or answered 5xx."""


class RefusedError(Exception):
    """The other service answered 4xx: status and its error code, passed on to the caller."""

    def __init__(self, status_code: int, error: str, message: str = ""):
        super().__init__(f"{status_code} {error}")
        self.status_code = status_code
        self.error = error
        self.message = message


def _refusal(response: httpx.Response) -> RefusedError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return RefusedError(
        response.status_code,
        str(body.get("error") or f"http_{response.status_code}"),
        str(body.get("message") or ""),
    )


def _send(http: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        response = http.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise UnavailableError(f"{method} {url}: {type(exc).__name__}") from exc
    if response.status_code >= 500:
        raise UnavailableError(f"{method} {url}: {response.status_code}")
    if response.status_code >= 400:
        raise _refusal(response)
    return response


class ServiceToken:
    """Orders' client-credentials token, fetched once and reused until shortly before expiry."""

    def __init__(self, http: httpx.Client, token_url: str, client_id: str, client_secret: str):
        self._http = http
        self._url = token_url
        self._auth = (client_id, client_secret)
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def get(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires - _EARLY:
                return self._token
            try:
                response = _send(
                    self._http,
                    "POST",
                    self._url,
                    data={"grant_type": "client_credentials"},
                    auth=self._auth,
                )
            except RefusedError as exc:
                # A refused client secret is a setup fault, not the caller's: retry later.
                raise UnavailableError(f"Keycloak refused Orders' token: {exc}") from exc
            body = response.json()
            if not body.get("access_token"):
                raise UnavailableError("Keycloak answered without an access_token")
            self._token = body["access_token"]
            self._expires = time.monotonic() + float(body.get("expires_in", 60))
            return self._token

    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get()}"}


@dataclass(frozen=True)
class Reservation:
    id: uuid.UUID
    expires_at: datetime
    price_cents: int
    seller_sub: uuid.UUID


@dataclass(frozen=True)
class Intent:
    payment_intent_id: str
    client_secret: str


class Listings:
    def __init__(self, http: httpx.Client, base_url: str, token: ServiceToken):
        self._http = http
        self._base = base_url.rstrip("/")
        self._token = token

    def reserve(
        self, listing_id: uuid.UUID, order_id: uuid.UUID, buyer_authorization: str
    ) -> Reservation:
        response = _send(
            self._http,
            "POST",
            f"{self._base}/listings/{listing_id}/reservations",
            json={"order_id": str(order_id)},
            headers={"Authorization": buyer_authorization},
        )
        body = response.json()
        return Reservation(
            id=uuid.UUID(body["id"]),
            expires_at=datetime.fromisoformat(body["expires_at"]),
            price_cents=int(body["price_cents"]),
            seller_sub=uuid.UUID(body["seller_sub"]),
        )

    def consume(self, reservation_id: uuid.UUID) -> None:
        """Marks the listing sold. RefusedError(409, reservation_released) if it was let go."""
        _send(
            self._http,
            "POST",
            f"{self._base}/reservations/{reservation_id}/consume",
            headers=self._token.header(),
        )

    def release(self, reservation_id: uuid.UUID) -> None:
        _send(
            self._http,
            "POST",
            f"{self._base}/reservations/{reservation_id}/release",
            headers=self._token.header(),
        )


class Payments:
    def __init__(self, http: httpx.Client, base_url: str, token: ServiceToken):
        self._http = http
        self._base = base_url.rstrip("/")
        self._token = token

    def create_intent(
        self, order_id: uuid.UUID, amount_cents: int, fee_pct: Decimal, seller_sub: uuid.UUID
    ) -> Intent:
        response = _send(
            self._http,
            "POST",
            f"{self._base}/payment-intents",
            json={
                "order_id": str(order_id),
                "amount_cents": amount_cents,
                "fee_pct": float(fee_pct),
                "seller_sub": str(seller_sub),
            },
            headers=self._token.header(),
        )
        body = response.json()
        return Intent(body["payment_intent_id"], body["client_secret"])

    def cancel_intent(self, payment_intent_id: str) -> None:
        _send(
            self._http,
            "POST",
            f"{self._base}/payment-intents/{payment_intent_id}/cancel",
            headers=self._token.header(),
        )
