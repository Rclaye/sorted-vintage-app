"""The marketplace as a browser sees it, from inside the cluster (Sprint 2 spike "In-cluster
sign-in", 2026-11-12).

A browser reaches https://dev.sortedvtg.lan:8443 and https://auth.sortedvtg.lan:8443 through the
workstation's SSH tunnel to Traefik. In a pod those names do not resolve, so LabTransport sends
any *.sortedvtg.lan request to Traefik's in-cluster Service on 443 and keeps the name for TLS
(SNI, hostname check) and for the Host header. The names stay what a browser sends, so
Keycloak's issuer, the BFF's redirect address and the cookies all match. TLS trusts only the
lab's own certificate (pinned; OpenSSL partial chain), never the system CAs.
"""

import re
import socket
import ssl
from collections.abc import Callable
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx

LAB_SUFFIX = ".sortedvtg.lan"
Log = Callable[[str], None]


class SeedError(Exception):
    pass


def pinned_context(cert_file: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=cert_file)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


class LabTransport(httpx.HTTPTransport):
    def __init__(self, traefik_host: str, resolve: Callable[[str], str] | None = None, **kw):
        super().__init__(**kw)
        self._traefik_host = traefik_host
        self._resolve = resolve or socket.gethostbyname

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host.endswith(LAB_SUFFIX):
            return super().handle_request(request)
        headers = httpx.Headers(request.headers)
        headers["host"] = host
        inner = httpx.Request(
            request.method,
            request.url.copy_with(host=self._resolve(self._traefik_host), port=443),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return super().handle_request(inner)


def where(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname or ''}{parts.path}"


class Site:
    """One signed-in browser session against the BFF."""

    def __init__(self, site_url: str, client: httpx.Client, log: Log):
        self.site_url = site_url
        self.client = client
        self.log = log
        self.csrf = ""

    def _go(self, method: str, url: str, **kw) -> httpx.Response:
        url = urljoin(self.site_url + "/", url)
        r = self.client.request(method, url, **kw)
        loc = r.headers.get("location", "")
        self.log(
            f"{method} {where(url)} -> {r.status_code} {where(urljoin(url, loc)) if loc else ''}"
        )
        return r

    def _location(self, r: httpx.Response, *codes: int) -> str:
        if r.status_code not in codes or "location" not in r.headers:
            raise SeedError(
                f"{r.request.method} {where(str(r.request.url))} answered {r.status_code}"
            )
        return urljoin(str(r.request.url), r.headers["location"])

    def sign_in(self, username: str, password: str) -> None:
        r = self._go("GET", "/login?next=/sell")
        r = self._go("GET", self._location(r, 303))
        m = re.search(r'action="([^"]*login-actions/authenticate[^"]*)"', r.text)
        if r.status_code != 200 or not m:
            raise SeedError("Keycloak did not show its login form")
        r = self._go(
            "POST",
            unescape(m.group(1)),
            data={"username": username, "password": password, "credentialId": ""},
        )
        if r.status_code == 200:
            raise SeedError(f"Keycloak refused the sign-in for {username}")
        r = self._go("GET", self._location(r, 302, 303))
        if urlsplit(self._location(r, 303)).path != "/sell":
            raise SeedError("the BFF did not finish the sign-in")
        self.refresh_csrf()

    def refresh_csrf(self) -> None:
        r = self._go("GET", "/sell")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        if r.status_code != 200 or not m:
            raise SeedError("the selling page has no form token (not signed in?)")
        self.csrf = m.group(1)

    def create_listing(self, fields: dict[str, str]) -> str:
        r = self._go("POST", "/sell/listings", data={"csrf_token": self.csrf, **fields})
        path = urlsplit(self._location(r, 303)).path
        if not path.startswith("/items/"):
            raise SeedError(f"the listing was refused: {r.headers['location']}")
        return path.removeprefix("/items/")

    def add_photo(self, listing_id: str, name: str, data: bytes) -> None:
        r = self._go(
            "POST",
            f"/sell/listings/{listing_id}/photos",
            data={"csrf_token": self.csrf},
            files={"file": (name, data, "image/png")},
        )
        if "error=" in self._location(r, 303):
            raise SeedError(f"the photo was refused: {r.headers['location']}")

    def publish(self, listing_id: str) -> None:
        r = self._go("POST", f"/sell/listings/{listing_id}/publish", data={"csrf_token": self.csrf})
        if "published=1" not in self._location(r, 303):
            raise SeedError(f"publishing was refused: {r.headers['location']}")
