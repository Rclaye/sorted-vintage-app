"""Application factory. Run with: uvicorn app.main:create_app --factory."""

import logging
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from app import signin
from app.auth import KeycloakKeys, KeyResolver
from app.config import Settings
from app.errors import error_response, install_error_handlers
from app.health import Check, checks_from_settings, failing
from app.logging_json import configure_logging
from app.metrics import MetricsMiddleware, make_histogram
from app.oidc import Keycloak
from app.session import SessionCodec
from app.telemetry import build_tracer_provider, instrument

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    keys: KeyResolver | None = None,
    checks: dict[str, Check] | None = None,
    tracer_provider: TracerProvider | None = None,
    keycloak: Keycloak | None = None,
) -> FastAPI:
    """Tests pass fakes for keys, checks and tracer_provider; production reads the environment."""
    settings = settings or Settings.from_env()
    settings.validate()
    configure_logging(settings.service_name, settings.env, settings.log_level)
    if checks is None:
        checks = checks_from_settings(settings.database_url, settings.amqp_url)
    if keys is None:
        if not settings.jwks_url:
            raise RuntimeError("JWKS_URL is not set: the service cannot verify tokens without it.")
        keys = KeycloakKeys(settings.jwks_url)

    app = FastAPI(
        title=settings.service_name,
        description="Browser-facing backend: OIDC session and server-rendered pages",
    )
    app.state.settings = settings
    app.state.codec = SessionCodec(settings.session_cookie_key)
    app.state.keycloak = keycloak or Keycloak(
        settings.realm_public_url,
        settings.realm_internal_url,
        settings.client_id,
        settings.client_secret,
        keys,
    )
    install_error_handlers(app)

    @app.exception_handler(signin.SignInRequiredError)
    async def _sign_in_first(_: Request, exc: signin.SignInRequiredError):
        return RedirectResponse(f"/login?next={quote(exc.next_path, safe='/')}", status_code=303)

    @app.exception_handler(signin.CsrfError)
    async def _csrf(_: Request, __: signin.CsrfError):
        return error_response(
            403, "csrf_failed", "The form expired. Reload the page and try again."
        )

    app.include_router(signin.router)

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

    # Browsers carry a session cookie, not a bearer token (contracts/bff-pages.md), so the
    # template's JWT middleware is not used here: pages call signin.require_session instead.
    @app.middleware("http")
    async def keep_session_cookie(request: Request, call_next):
        """Writes a refreshed session back, or clears one that can no longer be refreshed."""
        response = await call_next(request)
        if getattr(request.state, "session_cleared", False):
            signin.clear_session_cookie(request, response)
        elif getattr(request.state, "session_renewed", None) is not None:
            signin.set_session_cookie(request, response, request.state.session_renewed)
        return response

    # Last added is outermost: telemetry > metrics/logging > session cookie > routes.
    app.add_middleware(MetricsMiddleware, histogram=histogram)
    instrument(app, tracer_provider or build_tracer_provider(settings))
    return app
