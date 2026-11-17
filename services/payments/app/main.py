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

from app import charges as charges_routes
from app import connect as connect_routes
from app import inbox_worker, ledger, webhooks
from app.auth import JWTAuthMiddleware, KeycloakKeys, KeyResolver
from app.config import Settings
from app.db import make_engine, migrate
from app.errors import error_response, install_error_handlers
from app.health import Check, checks_from_settings, failing
from app.logging_json import configure_logging
from app.metrics import MetricsMiddleware, make_histogram
from app.orders_client import Orders
from app.reconcile import run_forever
from app.stripe_api import StripeCharges, StripeConnect, make_client
from app.telemetry import build_tracer_provider, instrument

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    keys: KeyResolver | None = None,
    checks: dict[str, Check] | None = None,
    tracer_provider: TracerProvider | None = None,
    engine: Engine | None = None,
    connect: StripeConnect | None = None,
) -> FastAPI:
    """Tests pass fakes for keys, checks, tracer_provider, engine and connect; production reads
    the environment. The database is migrated and the reconciler started when the app starts."""
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
    connect = connect or StripeConnect(make_client(settings.stripe_secret_key))
    charges = StripeCharges(connect.client)
    http = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0))
    orders = Orders(
        http,
        settings.orders_url,
        settings.token_url,
        settings.keycloak_client_id,
        settings.keycloak_client_secret,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(migrate, engine)
        tasks = []
        if settings.reconcile_interval_seconds > 0:
            tasks.append(
                asyncio.create_task(
                    run_forever(
                        engine,
                        connect,
                        settings.reconcile_interval_seconds,
                        charges,
                        settings.pending_after_seconds,
                    )
                )
            )
        if settings.inbox_interval_seconds > 0:
            tasks.append(
                asyncio.create_task(
                    inbox_worker.run_forever(
                        engine, orders, charges, settings.inbox_interval_seconds
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
        description="Stripe Connect onboarding, payout status, webhooks and reconciliation (Accounts v2)",  # noqa: E501
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.connect = connect
    app.state.charges = charges
    app.state.orders = orders
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

    # Each route needs a valid Keycloak token unless listed in PUBLIC_PATHS (the webhooks are).
    app.include_router(connect_routes.router)
    app.include_router(charges_routes.router)
    app.include_router(ledger.router)
    app.include_router(webhooks.router)

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
    instrument(app, tracer_provider or build_tracer_provider(settings))
    return app
