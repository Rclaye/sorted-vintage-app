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
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Not set: {', '.join(missing)}. Payments cannot run without them.")

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
            reconcile_interval_seconds=int(
                env.get("RECONCILE_INTERVAL_SECONDS", defaults.reconcile_interval_seconds)
            ),
        )
