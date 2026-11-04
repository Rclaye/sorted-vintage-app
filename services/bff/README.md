# sv-bff

Browser-facing backend: OIDC session and server-rendered pages (stub: /healthz and /metrics)

Generated from `templates/service-fastapi` (see `.copier-answers.yml`). Pull in template
changes with `copier update`.

## Contract every route follows

- `GET /healthz`: 200 `{"status":"ok"}`, or 503 when a dependency is unreachable. No token.
- `GET /metrics`: Prometheus text, `http_server_request_duration_seconds{method,route,status}`. No token.
- Everything else needs `Authorization: Bearer <Keycloak JWT>` with audience `bff`; otherwise 401.
- Errors: `{"error": "<machine_code>", "message": "<human text>"}`. Raise `app.errors.ApiError`.
- Logs: JSON lines with `timestamp`, `level`, `service`, `env`, `trace_id`, `span_id`, `message`.

## Configuration (environment)

| Variable | Meaning |
| --- | --- |
| `ENV` | `dev`, `prod` or `local`; appears in every log line |
| `JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE` | Keycloak realm keys, issuer and this service's audience |
| `DATABASE_URL`, `AMQP_URL` | Dependencies `/healthz` probes (TCP connect); empty means not used |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP collector base URL; empty disables export |
| `PUBLIC_PATHS` | Comma-separated paths that skip the JWT check (default `/healthz,/metrics`) |

## Develop

```bash
uv venv --python 3.12 && uv pip install -r requirements-dev.txt
.venv/bin/pytest --cov --cov-report=xml
```

Update pins with `uv pip compile --universal --python-version 3.12 requirements.in -o requirements.txt`
(and the same for `requirements-dev.in`).
