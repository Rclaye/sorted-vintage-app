"""Who to write to and what about: email addresses from Keycloak, titles from Listings.

Keycloak is read with Notifications' own client-credentials token, whose service account holds
realm-management view-users only (spikes 3.1). The address is read at send time and never
stored. A listing's title is read from Listings' public GET; events carry none (research R8), and
an email without the title is still worth sending, so any failure there gives None.
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

# Renew the service token this many seconds before Keycloak says it expires.
_EARLY = 30


class AddressError(Exception):
    """Keycloak could not give an address: unreachable, refused, or the user has none."""


class ServiceToken:
    """The client-credentials token, fetched once and reused until shortly before expiry."""

    def __init__(self, http: httpx.AsyncClient, token_url: str, client_id: str, secret: str):
        self._http = http
        self._url = token_url
        self._auth = (client_id, secret)
        self._lock = asyncio.Lock()
        self._token = ""
        self._expires = 0.0

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires - _EARLY:
                return self._token
            try:
                response = await self._http.post(
                    self._url, data={"grant_type": "client_credentials"}, auth=self._auth
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise AddressError(f"service token: {type(exc).__name__}") from exc
            self._token = str(body["access_token"])
            self._expires = time.monotonic() + float(body.get("expires_in", 60))
            return self._token


class KeycloakUsers:
    """sub -> email through the admin API of this environment's realm."""

    def __init__(self, http: httpx.AsyncClient, admin_url: str, token: ServiceToken):
        self._http = http
        self._admin = admin_url.rstrip("/")
        self._token = token

    async def email_of(self, sub: str) -> str:
        token = await self._token.get()
        try:
            response = await self._http.get(
                f"{self._admin}/users/{sub}", headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise AddressError(f"user {sub}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise AddressError(f"user {sub}: {response.status_code}")
        email = response.json().get("email")
        if not email:
            raise AddressError(f"user {sub}: no email")
        return str(email)


class ListingTitles:
    """listing_id -> title, best effort."""

    def __init__(self, http: httpx.AsyncClient, listings_url: str):
        self._http = http
        self._url = listings_url.rstrip("/")

    async def title_of(self, listing_id: str) -> str | None:
        try:
            response = await self._http.get(f"{self._url}/listings/{listing_id}")
            if response.status_code == 200:
                title = response.json().get("title")
                return str(title) if title else None
        except (httpx.HTTPError, ValueError) as exc:
            log.info("listing title not read", extra={"reason": type(exc).__name__})
        return None
