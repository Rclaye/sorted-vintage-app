"""Fakes for the tests: a throw-away signing key, in-memory spans, and stub dependencies."""

import io
import json
import logging
import time
from collections.abc import Callable

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.logging_json import JsonFormatter
from app.main import create_app

# What this service is configured to demand: an audience, or (BFF) an authorized party.
AUDIENCE = Settings().jwt_audience
AZP = Settings().jwt_azp
ISSUER = "http://keycloak.test/realms/sv-test"


class StaticKeys:
    """Stands in for Keycloak's JWKS: one public key, no network."""

    def __init__(self, public_key):
        self.public_key = public_key

    def signing_key(self, token: str):
        return self.public_key


@pytest.fixture(scope="session")
def private_key():
    # Generated per run so no key material is committed.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def make_token(private_key) -> Callable[..., str]:
    def make(signing_key=None, **overrides) -> str:
        now = int(time.time())
        claims = {
            "sub": "user-1",
            "typ": "Bearer",
            "aud": AUDIENCE or ["listings", "orders", "payments"],
            "azp": AZP or "some-client",
            "iss": ISSUER,
            "iat": now,
            "exp": now + 300,
        }
        claims.update({k: v for k, v in overrides.items() if v is not None})
        for name in [k for k, v in overrides.items() if v is None]:
            claims.pop(name, None)
        return jwt.encode(claims, signing_key or private_key, algorithm="RS256")

    return make


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        jwt_issuer=ISSUER,
        log_level="INFO",
        # Never connected to by these tests: the engine connects lazily and lifespan is not run.
        database_url="postgresql://test:test@127.0.0.1:1/test",
        stripe_secret_key="sk_test_not_used",
        stripe_webhook_secret="whsec_not_used",
        public_base_url="https://dev.sortedvtg.lan:8443",
        reconcile_interval_seconds=0,
    )


@pytest.fixture
def checks() -> dict:
    """Healthy by default; tests replace entries with failing ones."""

    async def up() -> None:
        return None

    return {"database": up, "queue": up}


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def client(settings, private_key, checks, spans) -> TestClient:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    app = create_app(
        settings,
        keys=StaticKeys(private_key.public_key()),
        checks=checks,
        tracer_provider=provider,
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def log_lines(client, settings):
    """JSON log lines written from now on, parsed. Call it to read them."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(settings.service_name, settings.env))
    logging.getLogger().addHandler(handler)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    logging.getLogger().removeHandler(handler)
