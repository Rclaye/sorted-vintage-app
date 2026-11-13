"""Prometheus metrics and the RED middleware behind http_server_request_duration_seconds."""

import logging
import time

from prometheus_client import CollectorRegistry, Histogram
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("app.request")

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def make_histogram(registry: CollectorRegistry) -> Histogram:
    return Histogram(
        "http_server_request_duration_seconds",
        "HTTP server request duration in seconds.",
        ["method", "route", "status"],
        buckets=BUCKETS,
        registry=registry,
    )


def _route_template(scope: Scope) -> str:
    """The route pattern (/items/{id}), never the raw path, to keep label cardinality bounded."""
    route = scope.get("route")
    if route is not None:
        return route.path
    # Rejected before routing (for example by the JWT middleware): look the route up.
    app = scope.get("app")
    for candidate in getattr(app, "routes", []):
        if candidate.matches(scope)[0] == Match.FULL:
            return candidate.path
    return "unmatched"


class MetricsMiddleware:
    """Outermost app middleware: times every request and writes one log line for it."""

    def __init__(self, app: ASGIApp, histogram: Histogram, skip: tuple[str, ...] = ("/metrics",)):
        self.app = app
        self.histogram = histogram
        self.skip = skip

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.skip:
            await self.app(scope, receive, send)
            return

        status = 500
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _route_template(scope)
            self.histogram.labels(scope["method"], route, str(status)).observe(elapsed)
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
