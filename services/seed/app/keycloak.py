"""The seed client's view of Keycloak: its own token, and the users it may manage (T120)."""

import httpx

from app.config import Seller, Settings
from app.site import Log, SeedError


class Keycloak:
    def __init__(self, settings: Settings, http: httpx.Client, log: Log):
        self.s = settings
        self.http = http
        self.log = log
        self.admin = f"{settings.keycloak_url}/admin/realms/{settings.realm}"

    def token(self) -> str:
        r = self.http.post(
            f"{self.s.keycloak_url}/realms/{self.s.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.s.client_id,
                "client_secret": self.s.client_secret,
            },
        )
        self.log(f"POST keycloak/realms/{self.s.realm}/token -> {r.status_code}")
        if r.status_code != 200:
            raise SeedError("the seed client could not get a token")
        return r.json()["access_token"]

    def ensure_user(self, seller: Seller, token: str) -> str:
        """Create the seller, or bring an existing one back to the declared state; returns sub."""
        auth = {"Authorization": f"Bearer {token}"}
        first, _, last = seller.display_name.rpartition(" ")
        profile = {
            "username": seller.key,
            "email": seller.email,
            "emailVerified": True,
            "enabled": True,
            "firstName": first or seller.display_name,
            "lastName": last,
            "requiredActions": [],
            "attributes": {"display_name": [seller.display_name]},
        }
        password = {"type": "password", "value": seller.password, "temporary": False}
        r = self.http.get(
            f"{self.admin}/users", params={"username": seller.key, "exact": "true"}, headers=auth
        )
        r.raise_for_status()
        found = r.json()
        if found:
            sub = found[0]["id"]
            u = self.http.put(f"{self.admin}/users/{sub}", json=profile, headers=auth)
            p = self.http.put(
                f"{self.admin}/users/{sub}/reset-password", json=password, headers=auth
            )
            self.log(f"PUT keycloak/users/{seller.key} -> {u.status_code}")
            if u.status_code != 204 or p.status_code != 204:
                raise SeedError(f"could not update {seller.key} ({u.status_code}/{p.status_code})")
            return sub
        r = self.http.post(
            f"{self.admin}/users", json={**profile, "credentials": [password]}, headers=auth
        )
        self.log(f"POST keycloak/users/{seller.key} -> {r.status_code}")
        if r.status_code != 201:
            raise SeedError(f"could not create {seller.key} ({r.status_code})")
        return r.headers["location"].rstrip("/").rsplit("/", 1)[-1]
