"""Settings, read once from environment variables."""

import os
from dataclasses import dataclass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    service_name: str = "bff"
    env: str = "local"
    log_level: str = "INFO"
    # Keycloak: tokens must be signed by a key in jwks_url. Then either aud must contain
    # jwt_audience, or (jwt_audience empty) azp must equal jwt_azp.
    jwt_audience: str = ""
    jwt_azp: str = "bff"
    jwt_issuer: str = ""
    jwks_url: str = ""
    # Dependencies that /healthz probes with a TCP connect. Empty means not used.
    database_url: str = ""
    amqp_url: str = ""
    # OTLP/HTTP collector, e.g. http://otel-collector.monitoring:4318. Empty disables export.
    otlp_endpoint: str = ""
    public_paths: tuple[str, ...] = ("/healthz", "/metrics")
    # Sign-in (T047). Keycloak's browser-facing realm URL is jwt_issuer; the BFF talks to the
    # realm in-cluster at the URL jwks_url sits under. The client secret is keycloak-clients/bff.
    client_id: str = "bff"
    client_secret: str = ""
    # This environment's address as the browser sees it, e.g. https://dev.sortedvtg.lan:8443.
    public_base_url: str = ""
    # Secret bff-session/cookie-key (Terraform): encrypts the session cookie.
    session_cookie_key: str = ""
    # Secure cookies need HTTPS; only local runs without TLS turn this off.
    cookie_secure: bool = True

    @property
    def realm_public_url(self) -> str:
        return self.jwt_issuer.rstrip("/")

    @property
    def realm_internal_url(self) -> str:
        """The realm's in-cluster base URL: jwks_url without /protocol/openid-connect/certs."""
        return self.jwks_url.split("/protocol/openid-connect/")[0].rstrip("/")

    def validate(self) -> None:
        """Fail closed: an empty issuer, audience or azp would silently switch a check off."""
        if not self.jwt_issuer:
            raise RuntimeError("JWT_ISSUER is not set: tokens cannot be verified without it.")
        if not (self.jwt_audience or self.jwt_azp):
            raise RuntimeError("Set JWT_AUDIENCE or JWT_AZP: a service must check one of them.")
        missing = [
            name
            for name, value in (
                ("JWKS_URL", self.jwks_url),
                ("KEYCLOAK_CLIENT_SECRET", self.client_secret),
                ("PUBLIC_BASE_URL", self.public_base_url),
                ("SESSION_COOKIE_KEY", self.session_cookie_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Not set: {', '.join(missing)}. Sign-in cannot work without them.")
        if len(self.session_cookie_key) < 32:
            raise RuntimeError("SESSION_COOKIE_KEY is too short: use at least 32 characters.")

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
            client_id=env.get("KEYCLOAK_CLIENT_ID", defaults.client_id),
            client_secret=env.get("KEYCLOAK_CLIENT_SECRET", ""),
            public_base_url=env.get("PUBLIC_BASE_URL", "").rstrip("/"),
            session_cookie_key=env.get("SESSION_COOKIE_KEY", ""),
            cookie_secure=env.get("COOKIE_SECURE", "true").lower() != "false",
        )
