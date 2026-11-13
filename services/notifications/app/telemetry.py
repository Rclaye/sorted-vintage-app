"""OpenTelemetry: tracer provider, OTLP/HTTP export, W3C traceparent in and out."""

from fastapi import FastAPI
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
