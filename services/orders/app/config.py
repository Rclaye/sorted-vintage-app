"""Settings, read once from environment variables."""

import os
from dataclasses import dataclass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    service_name: str = "orders"
    env: str = "local"
    log_level: str = "INFO"
    # Keycloak: tokens must be signed by a key in jwks_url. Then either aud must contain
    # jwt_audience, or (jwt_audience empty) azp must equal jwt_azp.
    jwt_audience: str = "orders"
    jwt_azp: str = ""
    jwt_issuer: str = ""
    jwks_url: str = ""
    # Dependencies that /healthz probes with a TCP connect. Empty means not used.
    database_url: str = ""
    amqp_url: str = ""
    # OTLP/HTTP collector, e.g. http://otel-collector.monitoring:4318. Empty disables export.
    otlp_endpoint: str = ""
    public_paths: tuple[str, ...] = ("/healthz", "/metrics")
    # The services Orders calls (contracts/listings.openapi.yaml, payments.openapi.yaml).
    listings_url: str = "http://listings"
    payments_url: str = "http://payments"
    # Orders' own client-credentials token for Payments and for consume/release in Listings
    # (research R8). The token URL is in-cluster; empty means the issuer's own endpoint.
    keycloak_token_url: str = ""
    keycloak_client_id: str = "orders"
    keycloak_client_secret: str = ""
    # The one client allowed to post payment results (T063).
    payments_azp: str = "payments"
    # The platform fee recorded on each order when checkout starts (FR-013).
    fee_pct: str = "10.00"
    # Seconds between expiry runs (T064) and outbox relay polls (T065); 0 turns a loop off (tests).
    expiry_interval_seconds: float = 30.0
    relay_interval_seconds: float = 1.0

    def validate(self) -> None:
        """Fail closed: an empty issuer, audience or azp would silently switch a check off."""
        if not self.jwt_issuer:
            raise RuntimeError("JWT_ISSUER is not set: tokens cannot be verified without it.")
        if not (self.jwt_audience or self.jwt_azp):
            raise RuntimeError("Set JWT_AUDIENCE or JWT_AZP: a service must check one of them.")
        missing = [
            name
            for name, value in (
                ("DATABASE_URL", self.database_url),
                ("AMQP_URL", self.amqp_url),
                ("KEYCLOAK_CLIENT_SECRET", self.keycloak_client_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Not set: {', '.join(missing)}. Orders cannot run without them.")

    @property
    def token_url(self) -> str:
        return self.keycloak_token_url or (
            self.jwt_issuer.rstrip("/") + "/protocol/openid-connect/token"
        )

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        defaults = cls()
        return cls(
            service_name=env.get("SERVICE_NAME", defaults.service_name),
            env=env.get("ENV", defaults.env),
            log_level=env.get("LOG_LEVEL", defaults.log_level).upper(),
            jwt_audience=env.get("JWT_AUDIENCE", defaults.jwt_audience),
            jwt_azp=env.get("JWT_AZP", defaults.jwt_azp),
            jwt_issuer=env.get("JWT_ISSUER", ""),
            jwks_url=env.get("JWKS_URL", ""),
            database_url=env.get("DATABASE_URL", ""),
            amqp_url=env.get("AMQP_URL", ""),
            otlp_endpoint=env.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            public_paths=_csv(env.get("PUBLIC_PATHS", "")) or defaults.public_paths,
            listings_url=env.get("LISTINGS_URL", defaults.listings_url).rstrip("/"),
            payments_url=env.get("PAYMENTS_URL", defaults.payments_url).rstrip("/"),
            keycloak_token_url=env.get("KEYCLOAK_TOKEN_URL", ""),
            keycloak_client_id=env.get("KEYCLOAK_CLIENT_ID", defaults.keycloak_client_id),
            keycloak_client_secret=env.get("KEYCLOAK_CLIENT_SECRET", ""),
            payments_azp=env.get("PAYMENTS_AZP", defaults.payments_azp),
            fee_pct=env.get("FEE_PCT", defaults.fee_pct),
            expiry_interval_seconds=float(
                env.get("EXPIRY_INTERVAL_SECONDS", defaults.expiry_interval_seconds)
            ),
            relay_interval_seconds=float(
                env.get("RELAY_INTERVAL_SECONDS", defaults.relay_interval_seconds)
            ),
        )
