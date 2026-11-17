"""Payments' one call to Orders: POST /orders/{order_id}/payment-result (research R8), with
Payments' own client-credentials token. Anything but a 200 means "try again later": Orders
answers 404 for an order it does not know yet and 503 while Listings is unreachable.
"""

import threading
import time
import uuid

import httpx

# Renew the service token this many seconds before Keycloak says it expires.
_EARLY = 30


class OrdersUnavailableError(Exception):
    """Orders (or Keycloak) could not take the result now; the event stays unapplied."""


class Orders:
    def __init__(
        self, http: httpx.Client, base_url: str, token_url: str, client_id: str, client_secret: str
    ):
        self._http = http
        self._base = base_url.rstrip("/")
        self._token_url = token_url
        self._auth = (client_id, client_secret)
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def _bearer(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires - _EARLY:
                return self._token
            try:
                response = self._http.post(
                    self._token_url, data={"grant_type": "client_credentials"}, auth=self._auth
                )
                response.raise_for_status()
                body = response.json()
                self._token = body["access_token"]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise OrdersUnavailableError(f"no service token: {type(exc).__name__}") from exc
            self._expires = time.monotonic() + float(body.get("expires_in", 60))
            return self._token

    def payment_result(
        self,
        order_id: uuid.UUID,
        payment_intent_id: str,
        outcome: str,
        event_key: str,
        failure_reason: str | None = None,
    ) -> str:
        """Reports one outcome; returns the order's status after it (pending, paid, failed or
        refunded). Idempotent on Orders' side, so a retry is always safe."""
        body = {"payment_intent_id": payment_intent_id, "outcome": outcome, "event_key": event_key}
        if failure_reason:
            body["failure_reason"] = failure_reason
        try:
            response = self._http.post(
                f"{self._base}/orders/{order_id}/payment-result",
                json=body,
                headers={"Authorization": f"Bearer {self._bearer()}"},
            )
        except httpx.HTTPError as exc:
            raise OrdersUnavailableError(type(exc).__name__) from exc
        if response.status_code == 401:
            with self._lock:
                self._token = ""
        if response.status_code != 200:
            raise OrdersUnavailableError(f"Orders answered {response.status_code}")
        return str(response.json()["status"])
