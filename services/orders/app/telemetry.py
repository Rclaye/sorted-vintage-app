"""OpenTelemetry: tracer provider, OTLP/HTTP export, W3C traceparent and baggage in and out, and
order_id on spans and log lines (T083).

`tag_order(order_id)` marks what is being handled now: the current span gets an `order_id`
attribute, every later log line of the same request or task gets an `order_id` field (the
request's own access line included), and W3C baggage carries it on outgoing calls, so the next
service tags its side by itself (`tag_from_baggage`, called for every request). Loops that handle
one order after another wrap each in `order_scope(order_id)`, so nothing carries over to the next.
"""

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

from fastapi import FastAPI
from opentelemetry import baggage, context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings


def build_tracer_provider(settings: Settings) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.service_name, "deployment.environment": settings.env}
        )
    )
    if settings.otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def instrument(app: FastAPI, provider: TracerProvider) -> None:
    """Continue the incoming trace (traceparent) and pass it on in outgoing httpx calls."""
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls="healthz,metrics"
    )
    httpx_instrumentor = HTTPXClientInstrumentor()
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        httpx_instrumentor.instrument(tracer_provider=provider)


ORDER_ID = "order_id"

# The tags of the request or task being handled. One dict per request (MetricsMiddleware) or per
# order_scope: a dict rather than a plain value, so a tag set inside a sync route's worker thread
# (which runs in a copy of the request's context) is still seen by the request's own log line.
_TAGS: ContextVar[dict[str, str] | None] = ContextVar("sv_tags", default=None)


def current_tags() -> dict[str, str]:
    return _TAGS.get() or {}


def tag_order(order_id: object) -> None:
    """Marks the current span, later log lines and outgoing calls with this order (T083)."""
    if order_id is None or order_id == "":
        return
    value = str(order_id)
    tags = _TAGS.get()
    if tags is None:
        tags = {}
        _TAGS.set(tags)
    tags[ORDER_ID] = value
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(ORDER_ID, value)
    if baggage.get_baggage(ORDER_ID) != value:
        context.attach(baggage.set_baggage(ORDER_ID, value))


def tag_from_baggage() -> None:
    """An order_id another service passed along in W3C baggage, if any. Browsers can send baggage
    too, so only a UUID is taken: anything else is ignored rather than written into the logs."""
    value = baggage.get_baggage(ORDER_ID)
    if not isinstance(value, str):
        return
    try:
        tag_order(uuid.UUID(value))
    except ValueError:
        return


@contextlib.contextmanager
def tag_scope() -> Iterator[None]:
    """Fresh tags for one request or one unit of background work; the OpenTelemetry context
    (and any baggage attached inside) is restored when the block ends."""
    tags_token = _TAGS.set({})
    context_token = context.attach(context.get_current())
    try:
        yield
    finally:
        context.detach(context_token)
        _TAGS.reset(tags_token)


@contextlib.contextmanager
def order_scope(order_id: object = None) -> Iterator[None]:
    """For loops that handle one order after another: tag_order for this block only. Without an
    id, the block starts untagged and may call tag_order once it knows the order."""
    with tag_scope():
        tag_order(order_id)
        yield
