"""JWT validation middleware (FR-003): Keycloak signature, audience and expiry, else 401."""

import logging
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import error_response

log = logging.getLogger(__name__)

ALGORITHMS = ["RS256"]


class KeyResolver(Protocol):
    def signing_key(self, token: str) -> Any:
        """The public key that verifies this token. Raises jwt.PyJWTError if there is none."""


class KeycloakKeys:
    """Fetches and caches the realm's public keys from its JWKS endpoint."""

    def __init__(self, jwks_url: str):
        self._client = PyJWKClient(jwks_url, cache_keys=True, timeout=3)

    def signing_key(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


class AuthUnavailableError(Exception):
    """The key source cannot be reached, so the token can be neither accepted nor rejected."""


class JWTAuthMiddleware:
    """Every HTTP route needs `Authorization: Bearer <JWT>` except the public paths."""

    def __init__(
        self,
        app: ASGIApp,
        keys: KeyResolver,
        audience: str = "",
        authorized_party: str = "",
        issuer: str = "",
        public_paths: tuple[str, ...] = ("/healthz", "/metrics"),
    ):
        if not issuer or not (audience or authorized_party):
            raise ValueError("JWT issuer and an audience or authorized party are required.")
        self.app = app
        self.keys = keys
        self.audience = audience
        self.authorized_party = authorized_party
        self.issuer = issuer
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            response = self._unauthorized("missing_token", "A bearer token is required.")
        else:
            response = self._check(scope, token.strip())
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _check(self, scope: Scope, token: str):
        """Returns an error response, or None after storing the claims on the request."""
        try:
            key = self.keys.signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self.audience or None,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iss", "aud"] if self.audience else ["exp", "iss"],
                    "verify_aud": bool(self.audience),
                },
            )
            # Access tokens only: a Keycloak ID token has the same signature, aud and azp.
            if claims.get("typ") != "Bearer":
                raise jwt.InvalidTokenError("not an access token")
            if self.authorized_party and claims.get("azp") != self.authorized_party:
                raise jwt.InvalidTokenError("wrong authorized party")
        except jwt.ExpiredSignatureError:
            return self._unauthorized("token_expired", "The token has expired.")
        except jwt.PyJWKClientConnectionError:
            log.error("cannot reach the Keycloak key endpoint")
            return error_response(503, "auth_unavailable", "Cannot verify tokens right now.")
        except jwt.PyJWTError as exc:
            log.info("token rejected", extra={"reason": type(exc).__name__})
            return self._unauthorized("invalid_token", "The token is not valid for this service.")
        scope.setdefault("state", {})["claims"] = claims
        return None

    @staticmethod
    def _unauthorized(code: str, message: str):
        return error_response(401, code, message, {"WWW-Authenticate": "Bearer"})
