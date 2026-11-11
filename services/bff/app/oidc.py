"""Keycloak's OpenID Connect endpoints, as the BFF uses them (T047, FR-001).

The browser is sent to the realm's public URL (auth.sortedvtg.lan); the BFF itself talks to the
realm in-cluster, because pods cannot reach the browser address. Tokens are checked here before
a session is made: signature, issuer, azp == bff, and for the ID token the nonce.
"""

import base64
import contextlib
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

from app.auth import ALGORITHMS, KeyResolver


class SignInError(Exception):
    """The sign-in cannot be completed; the user is sent back to try again."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    id_token: str
    access_expires_at: float
    refresh_expires_at: float


def pkce_pair() -> tuple[str, str]:
    """A code verifier and its S256 challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


class Keycloak:
    def __init__(
        self,
        public_realm_url: str,
        internal_realm_url: str,
        client_id: str,
        client_secret: str,
        keys: KeyResolver,
        http: httpx.AsyncClient | None = None,
    ):
        self.public = public_realm_url.rstrip("/")
        self.internal = internal_realm_url.rstrip("/")
        self.client_id = client_id
        self._auth = (client_id, client_secret)
        self._keys = keys
        self._http = http or httpx.AsyncClient(timeout=5)

    def authorize_url(
        self, redirect_uri: str, state: str, challenge: str, nonce: str, signup: bool = False
    ) -> str:
        endpoint = "registrations" if signup else "auth"
        query = urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "scope": "openid email profile",
                "redirect_uri": redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.public}/protocol/openid-connect/{endpoint}?{query}"

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Tokens:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> Tokens:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def end_session(self, refresh_token: str) -> None:
        """Ends the Keycloak session too, so signing out here signs out everywhere."""
        # If Keycloak can't be reached the cookie is cleared anyway; its session then expires.
        with contextlib.suppress(httpx.HTTPError):
            await self._http.post(
                f"{self.internal}/protocol/openid-connect/logout",
                data={"refresh_token": refresh_token},
                auth=self._auth,
            )

    def check(self, token: str, nonce: str | None = None) -> dict:
        """The claims of a token issued to this BFF, or SignInError."""
        try:
            claims = jwt.decode(
                token,
                self._keys.signing_key(token),
                algorithms=ALGORITHMS,
                issuer=self.public,
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise SignInError(f"token rejected: {type(exc).__name__}") from exc
        if claims.get("azp") != self.client_id:
            raise SignInError("token was not issued to this client")
        if nonce is not None and claims.get("nonce") != nonce:
            raise SignInError("nonce does not match")
        return claims

    async def _token(self, form: dict) -> Tokens:
        try:
            response = await self._http.post(
                f"{self.internal}/protocol/openid-connect/token", data=form, auth=self._auth
            )
        except httpx.HTTPError as exc:
            raise SignInError(f"Keycloak unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SignInError(f"Keycloak answered {response.status_code}")
        body = response.json()
        now = time.time()
        return Tokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", ""),
            id_token=body.get("id_token", ""),
            access_expires_at=now + int(body.get("expires_in", 60)),
            refresh_expires_at=now + int(body.get("refresh_expires_in", 1800)),
        )
