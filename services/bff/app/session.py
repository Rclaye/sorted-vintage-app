"""The signed-in user's session, kept encrypted in an HTTP-only cookie (T047, decided 2026-11-11).

The cookie holds the Keycloak access and refresh tokens, so the BFF needs no session store and
any replica can serve any request. Fernet (AES-128-CBC with HMAC-SHA256) both encrypts and
authenticates it: a cookie that was changed or made with another key reads as signed out.
"""

import base64
import hashlib
import json
import secrets
import time
import zlib
from dataclasses import asdict, dataclass

from cryptography.fernet import Fernet, InvalidToken

SESSION_COOKIE = "sv_session"
LOGIN_COOKIE = "sv_login"
# Browsers keep cookies up to 4096 bytes including the name and attributes.
MAX_COOKIE_BYTES = 3900
LOGIN_SECONDS = 600


@dataclass
class Session:
    sub: str
    name: str
    email: str
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float
    csrf: str

    def access_expiring(self, within_seconds: float = 30) -> bool:
        return time.time() > self.access_expires_at - within_seconds

    def refresh_expired(self) -> bool:
        return time.time() > self.refresh_expires_at


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


class SessionCodec:
    """Turns sessions and sign-in state into cookie values and back."""

    def __init__(self, key_material: str):
        # Any long random string from the Secret becomes a Fernet key.
        digest = hashlib.sha256(key_material.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def _seal(self, payload: dict) -> str:
        return self._fernet.encrypt(zlib.compress(json.dumps(payload).encode())).decode()

    def _open(self, value: str | None, max_age: int | None = None) -> dict | None:
        if not value:
            return None
        try:
            return json.loads(zlib.decompress(self._fernet.decrypt(value.encode(), ttl=max_age)))
        except (InvalidToken, ValueError, zlib.error):
            return None

    def encode_session(self, session: Session) -> str:
        value = self._seal(asdict(session))
        if len(value) > MAX_COOKIE_BYTES:
            raise ValueError(f"session cookie would be {len(value)} bytes")
        return value

    def decode_session(self, value: str | None) -> Session | None:
        data = self._open(value)
        try:
            return Session(**data) if data else None
        except TypeError:
            return None

    def encode_login(self, state: str, verifier: str, nonce: str, next_path: str) -> str:
        return self._seal({"state": state, "verifier": verifier, "nonce": nonce, "next": next_path})

    def decode_login(self, value: str | None) -> dict | None:
        """The sign-in in progress, if it is younger than ten minutes."""
        return self._open(value, max_age=LOGIN_SECONDS)
