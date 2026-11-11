"""Calls from the BFF to Listings and Payments, with the signed-in user's access token (T048).

Every call returns the status and the JSON body (or the raw response for photos), so the pages
decide what an answer means; network failures become ServiceDown, shown as a friendly 503 page.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx


class ServiceDownError(Exception):
    def __init__(self, service: str):
        super().__init__(service)
        self.service = service


@dataclass(frozen=True)
class Answer:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error(self) -> str:
        return self.body.get("error", "") if isinstance(self.body, dict) else ""


class Services:
    def __init__(self, listings_url: str, payments_url: str, http: httpx.AsyncClient | None = None):
        self.listings = listings_url.rstrip("/")
        self.payments = payments_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=10)

    async def call(self, method: str, url: str, token: str | None = None, **kwargs: Any) -> Answer:
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        service = "Listings" if url.startswith(self.listings) else "Payments"
        try:
            response = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ServiceDownError(service) from exc
        if response.status_code >= 500:
            raise ServiceDownError(service)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        return Answer(response.status_code, body)

    async def photo(self, listing_id: UUID, photo_id: UUID, token: str | None) -> httpx.Response:
        """Photo bytes from Listings. Only parsed UUIDs go into the URL, so nothing the browser
        sends can point the request anywhere else."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self.listings}/photos/listings/{listing_id}/{photo_id}"
        try:
            return await self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ServiceDownError("Listings") from exc
