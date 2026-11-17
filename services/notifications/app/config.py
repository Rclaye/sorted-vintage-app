"""Settings, read once from environment variables."""

import os
from dataclasses import dataclass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    service_name: str = "notifications"
    env: str = "local"
    log_level: str = "INFO"
    # Keycloak: tokens must be signed by a key in jwks_url. Then either aud must contain
    # jwt_audience, or (jwt_audience empty) azp must equal jwt_azp.
    jwt_audience: str = "notifications"
    jwt_azp: str = ""
    jwt_issuer: str = ""
    jwks_url: str = ""
    # Dependencies that /healthz probes with a TCP connect. Empty means not used.
    database_url: str = ""
    amqp_url: str = ""
    # OTLP/HTTP collector, e.g. http://otel-collector.monitoring:4318. Empty disables export.
    otlp_endpoint: str = ""
    public_paths: tuple[str, ...] = ("/healthz", "/metrics")
    # Keycloak: Notifications' own client-credentials token (view-users only, spikes 3.1) to read
    # buyers' and sellers' email addresses. The token URL is in-cluster; empty means the issuer's.
    # The admin URL is .../admin/realms/<realm>; empty means derived from the token URL.
    keycloak_token_url: str = ""
    keycloak_admin_url: str = ""
    keycloak_client_id: str = "notifications"
    keycloak_client_secret: str = ""
    # Listings, read without a token for a listing's title (events carry none, research R8).
    listings_url: str = "http://listings"
    # SMTP: this environment's Mailpit. No authentication or TLS inside the cluster.
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    mail_from: str = "Sorted Vintage <no-reply@sortedvtg.lan>"
    # The queue declared by the Messaging Topology Operator (T070); 0 prefetch turns the consumer
    # off (tests).
    queue: str = "notifications.orders"
    prefetch: int = 10

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
            raise RuntimeError(
                f"Not set: {', '.join(missing)}. Notifications cannot run without them."
            )

    @property
    def token_url(self) -> str:
        return self.keycloak_token_url or (
            self.jwt_issuer.rstrip("/") + "/protocol/openid-connect/token"
        )

    @property
    def admin_url(self) -> str:
        """.../realms/<realm>/protocol/openid-connect/token -> .../admin/realms/<realm>."""
        if self.keycloak_admin_url:
            return self.keycloak_admin_url.rstrip("/")
        base, _, rest = self.token_url.partition("/realms/")
        realm = rest.split("/", 1)[0]
        return f"{base}/admin/realms/{realm}"

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
            keycloak_token_url=env.get("KEYCLOAK_TOKEN_URL", ""),
            keycloak_admin_url=env.get("KEYCLOAK_ADMIN_URL", ""),
            keycloak_client_id=env.get("KEYCLOAK_CLIENT_ID", defaults.keycloak_client_id),
            keycloak_client_secret=env.get("KEYCLOAK_CLIENT_SECRET", ""),
            listings_url=env.get("LISTINGS_URL", defaults.listings_url).rstrip("/"),
            smtp_host=env.get("SMTP_HOST", defaults.smtp_host),
            smtp_port=int(env.get("SMTP_PORT", defaults.smtp_port)),
            mail_from=env.get("MAIL_FROM", defaults.mail_from),
            queue=env.get("QUEUE", defaults.queue),
            prefetch=int(env.get("PREFETCH", defaults.prefetch)),
        )
