"""Settings, read once from environment variables."""

import os
from dataclasses import dataclass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    service_name: str = "payments"
    env: str = "local"
    log_level: str = "INFO"
    # Keycloak: tokens must be signed by a key in jwks_url. Then either aud must contain
    # jwt_audience, or (jwt_audience empty) azp must equal jwt_azp.
    jwt_audience: str = "payments"
    jwt_azp: str = ""
    jwt_issuer: str = ""
    jwks_url: str = ""
    # Dependencies that /healthz probes with a TCP connect. Empty means not used.
    database_url: str = ""
    amqp_url: str = ""
    # OTLP/HTTP collector, e.g. http://otel-collector.monitoring:4318. Empty disables export.
    otlp_endpoint: str = ""
    # The webhook routes carry a Stripe-Signature instead of a JWT (contracts/README.md).
    public_paths: tuple[str, ...] = (
        "/healthz",
        "/metrics",
        "/webhooks/stripe",
        "/webhooks/stripe-connect",
    )
    # Stripe: the environment's sandbox key and the signing secret its stripe listen uses
    # (research R3, R4), from the Terraform-created Secret stripe.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Where Stripe's hosted onboarding sends the seller back, e.g. https://dev.sortedvtg.lan:8443
    # (Ticket 2.1: the port is needed to reach the lab from the workstation).
    public_base_url: str = ""
    # Seconds between onboarding reconciliation runs (research R5); 0 turns the loop off (tests).
    reconcile_interval_seconds: int = 300
    # After an account event leaves the seller not ready, read the account once more this many
    # seconds later (Stripe's account read can lag its events slightly).
    event_recheck_seconds: float = 10.0
    # The one client allowed to attach a seller to an existing Stripe account through
    # PUT /connected-accounts/{sub} (T051: the dev seed job). Empty, the default, means the
    # route answers 404 for everyone; it is set in sv-dev only.
    account_link_azp: str = ""
    # Ticket 3.5. The one client allowed to create and cancel PaymentIntents.
    orders_azp: str = "orders"
    # Where the inbox worker reports payment outcomes, with Payments' own client-credentials
    # token (research R8). The token URL is in-cluster; empty means the issuer's own endpoint.
    orders_url: str = "http://orders"
    keycloak_token_url: str = ""
    keycloak_client_id: str = "payments"
    keycloak_client_secret: str = ""
    # Seconds between inbox worker passes; 0 turns the worker off (tests).
    inbox_interval_seconds: float = 2.0
    # T069: a PaymentIntent whose order is still open after this long is asked about at Stripe.
    pending_after_seconds: float = 120.0

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
                ("STRIPE_SECRET_KEY", self.stripe_secret_key),
                ("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret),
                ("PUBLIC_BASE_URL", self.public_base_url),
                ("KEYCLOAK_CLIENT_SECRET", self.keycloak_client_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Not set: {', '.join(missing)}. Payments cannot run without them.")

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
            stripe_secret_key=env.get("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=env.get("STRIPE_WEBHOOK_SECRET", ""),
            public_base_url=env.get("PUBLIC_BASE_URL", "").rstrip("/"),
            account_link_azp=env.get("ACCOUNT_LINK_AZP", "").strip(),
            orders_azp=env.get("ORDERS_AZP", defaults.orders_azp),
            orders_url=env.get("ORDERS_URL", defaults.orders_url).rstrip("/"),
            keycloak_token_url=env.get("KEYCLOAK_TOKEN_URL", ""),
            keycloak_client_id=env.get("KEYCLOAK_CLIENT_ID", defaults.keycloak_client_id),
            keycloak_client_secret=env.get("KEYCLOAK_CLIENT_SECRET", ""),
            inbox_interval_seconds=float(
                env.get("INBOX_INTERVAL_SECONDS", defaults.inbox_interval_seconds)
            ),
            pending_after_seconds=float(
                env.get("PENDING_AFTER_SECONDS", defaults.pending_after_seconds)
            ),
            event_recheck_seconds=float(
                env.get("EVENT_RECHECK_SECONDS", defaults.event_recheck_seconds)
            ),
            reconcile_interval_seconds=int(
                env.get("RECONCILE_INTERVAL_SECONDS", defaults.reconcile_interval_seconds)
            ),
        )
