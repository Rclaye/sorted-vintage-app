"""Application factory. Run with: uvicorn app.main:create_app --factory."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from sqlalchemy import Engine

from app import consumer as consumer_module
from app.auth import JWTAuthMiddleware, KeycloakKeys, KeyResolver
from app.config import Settings
from app.consumer import Consumer
from app.db import make_engine, migrate
from app.errors import error_response, install_error_handlers
from app.health import Check, checks_from_settings, failing
from app.logging_json import configure_logging
from app.mailer import SmtpMailer
from app.metrics import MetricsMiddleware, make_histogram
from app.people import KeycloakUsers, ListingTitles, ServiceToken
from app.store import PgStore
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
    environment. The database is migrated and the consumer started when the app starts."""
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
    registry = CollectorRegistry()
    histogram = make_histogram(registry)
    emails = Counter(
        "notifications_emails",
        "Email send attempts by template and result (sent, failed)",
        ["template", "result"],
        registry=registry,
    )

    http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    token = ServiceToken(
        http, settings.token_url, settings.keycloak_client_id, settings.keycloak_client_secret
    )
    consumer = Consumer(
        mailer=SmtpMailer(settings.smtp_host, settings.smtp_port, settings.mail_from),
        users=KeycloakUsers(http, settings.admin_url, token),
        store=PgStore(engine),
        titles=ListingTitles(http, settings.listings_url),
        on_send=lambda template, result: emails.labels(template, result).inc(),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(migrate, engine)
        task = None
        if settings.prefetch > 0:
            task = asyncio.create_task(
                consumer_module.run_forever(
                    consumer,
                    settings.amqp_url,
                    settings.queue,
                    settings.prefetch,
                    tracer_provider.get_tracer("notifications.consumer"),
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await http.aclose()
            engine.dispose()

    app = FastAPI(
        lifespan=lifespan,
        title=settings.service_name,
        description="Order emails to buyers and sellers, once per event",  # noqa: E501
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.consumer = consumer
    install_error_handlers(app)

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

    # No routes of its own: Notifications only consumes notifications.orders.

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
