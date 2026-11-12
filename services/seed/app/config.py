"""Settings from the environment, set by the Job in sorted-vintage-gitops services/seed."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

VERIFY_SELLER = "verify-seller"


@dataclass(frozen=True)
class Seller:
    key: str
    email: str
    stripe_account_id: str
    password: str

    @property
    def display_name(self) -> str:
        # demo-seller-1 -> Demo Seller 1
        return " ".join(part.capitalize() for part in self.key.split("-"))

    @property
    def is_demo(self) -> bool:
        return self.key != VERIFY_SELLER


@dataclass(frozen=True)
class Settings:
    keycloak_url: str  # in-cluster, for the seed client's own token and the admin API
    realm: str
    client_id: str
    client_secret: str
    payments_url: str
    listings_url: str
    site_url: str  # the BFF as a browser reaches it: https://dev.sortedvtg.lan:8443
    traefik_host: str  # where the browser names lead inside the cluster
    tls_cert_file: str  # the lab's certificate for *.sortedvtg.lan, pinned
    listings_total: int
    sellers: tuple[Seller, ...]


def load_sellers(path: str) -> tuple[Seller, ...]:
    raw = json.loads(Path(path).read_text())
    return tuple(
        Seller(
            key=str(s["key"]),
            email=str(s["email"]),
            stripe_account_id=str(s["stripe_account_id"]),
            password=str(s["password"]),
        )
        for s in raw
    )


def with_verify_seller(sellers: tuple[Seller, ...], verify_dir: str) -> tuple[Seller, ...]:
    """Add the verify seller from Secret verify-seller when seed-sellers does not list it yet.

    Its Stripe account can only be onboarded by hand once the user exists, and seed_sellers in
    Terraform only takes real account ids, so on the first run it is created without a link.
    """
    folder = Path(verify_dir)
    if any(s.key == VERIFY_SELLER for s in sellers) or not (folder / "password").is_file():
        return sellers
    email = (folder / "username").read_text().strip()
    password = (folder / "password").read_text().strip()
    return (*sellers, Seller(VERIFY_SELLER, email, "", password))


def from_env(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    sellers = load_sellers(env.get("SELLERS_FILE", "/etc/seed/sellers/sellers.json"))
    return Settings(
        keycloak_url=env.get("KEYCLOAK_URL", "http://keycloak-http.keycloak.svc.cluster.local"),
        realm=env.get("KEYCLOAK_REALM", "sv-dev"),
        client_id=env.get("KEYCLOAK_CLIENT_ID", "seed"),
        client_secret=env["KEYCLOAK_CLIENT_SECRET"],
        payments_url=env.get("PAYMENTS_URL", "http://payments"),
        listings_url=env.get("LISTINGS_URL", "http://listings"),
        site_url=env.get("SITE_URL", "https://dev.sortedvtg.lan:8443").rstrip("/"),
        traefik_host=env.get("TRAEFIK_HOST", "traefik.traefik.svc.cluster.local"),
        tls_cert_file=env.get("TLS_CERT_FILE", "/etc/seed/tls/tls.crt"),
        listings_total=int(env.get("LISTINGS_TOTAL", "50")),
        sellers=with_verify_seller(sellers, env.get("VERIFY_DIR", "/etc/seed/verify")),
    )
