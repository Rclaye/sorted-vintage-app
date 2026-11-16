"""Application factory. Run with: uvicorn app.main:create_app --factory."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from sqlalchemy import Engine

from app import checkout, expiry, outbox_relay, payment_result
from app.auth import JWTAuthMiddleware, KeycloakKeys, KeyResolver
from app.clients import Listings, Payments, ServiceToken
from app.config import Settings
from app.db import make_engine, migrate
from app.errors import error_response, install_error_handlers
from app.health import Check, checks_from_settings, failing
from app.logging_json import configure_logging
from app.metrics import MetricsMiddleware, make_histogram
from app.telemetry import build_tracer_provider, instrument

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    keys: KeyResolver | None = None,
    checks: dict[str, Check] | None = None,
    tracer_provider: TracerProvider | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Tests pass fakes for keys, checks, tracer_provider and engine; production reads the
    environment. The database is migrated, and the expiry loop and outbox relay started, when
    the app starts."""
    settings = settings or Settings.from_env()
    settings.validate()
    configure_logging(settings.service_name, settings.env, settings.log_level)
    if checks is None:
        checks = checks_from_settings(settings.database_url, settings.amqp_url)
    if keys is None:
        if not settings.jwks_url:
            raise RuntimeError("JWKS_URL is not set: the service cannot verify tokens without it.")
        keys = KeycloakKeys(settings.jwks_url)

    engine = engine or make_engine(settings.database_url)
    tracer_provider = tracer_provider or build_tracer_provider(settings)
    http = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0))
    token = ServiceToken(
        http, settings.token_url, settings.keycloak_client_id, settings.keycloak_client_secret
    )
    listings = Listings(http, settings.listings_url, token)
    payments = Payments(http, settings.payments_url, token)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(migrate, engine)
        tasks = []
        if settings.expiry_interval_seconds > 0:
            tasks.append(
                asyncio.create_task(
                    expiry.run_forever(
                        engine, listings, payments, settings.env, settings.expiry_interval_seconds
                    )
                )
            )
        if settings.relay_interval_seconds > 0:
            tasks.append(
                asyncio.create_task(
                    outbox_relay.run_forever(
                        engine,
                        settings.amqp_url,
                        settings.relay_interval_seconds,
                        tracer_provider.get_tracer("orders.outbox_relay"),
                    )
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            http.close()
            engine.dispose()

    app = FastAPI(
        lifespan=lifespan,
        title=settings.service_name,
        description="Checkout, order state, expiry and the outbox to RabbitMQ",  # noqa: E501
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.listings = listings
    app.state.payments = payments
    install_error_handlers(app)

    registry = CollectorRegistry()
    histogram = make_histogram(registry)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Response:
        down = await failing(checks)
        if down:
            return error_response(
                503, "dependency_unavailable", f"Not reachable: {', '.join(down)}."
            )
        return JSONResponse({"status": "ok"})

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    # Each route needs a valid Keycloak token unless listed in PUBLIC_PATHS.
    app.include_router(checkout.router)
    app.include_router(payment_result.router)

    # Last added is outermost: telemetry > metrics/logging > JWT > routes.
    app.add_middleware(
        JWTAuthMiddleware,
        keys=keys,
        audience=settings.jwt_audience,
        authorized_party=settings.jwt_azp,
        issuer=settings.jwt_issuer,
        public_paths=settings.public_paths,
    )
    app.add_middleware(MetricsMiddleware, histogram=histogram)
    instrument(app, tracer_provider)
    return app
