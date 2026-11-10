# sv-payments

Stripe Connect onboarding, payout status, webhooks and reconciliation (Accounts v2)

Generated from `templates/service-fastapi` (see `.copier-answers.yml`). Pull in template
changes with `copier update`.

## Contract every route follows

- `GET /healthz`: 200 `{"status":"ok"}`, or 503 when a dependency is unreachable. No token.
- `GET /metrics`: Prometheus text, `http_server_request_duration_seconds{method,route,status}`. No token.
- Everything else needs `Authorization: Bearer <Keycloak JWT>` with audience `payments`; otherwise 401.
- Errors: `{"error": "<machine_code>", "message": "<human text>"}`. Raise `app.errors.ApiError`.
- Logs: JSON lines with `timestamp`, `level`, `service`, `env`, `trace_id`, `span_id`, `message`.

## Configuration (environment)

| Variable | Meaning |
| --- | --- |
| `ENV` | `dev`, `prod` or `local`; appears in every log line |
| `JWKS_URL`, `JWT_ISSUER` | Keycloak realm keys and issuer |
| `JWT_AUDIENCE` or `JWT_AZP` | Required audience, or required authorized party (`token_check`) |
| `DATABASE_URL`, `AMQP_URL` | Dependencies `/healthz` probes (TCP connect); empty means not used |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP collector base URL; empty disables export |
| `PUBLIC_PATHS` | Comma-separated paths that skip the JWT check (default `/healthz,/metrics` and the two webhook routes, which check `Stripe-Signature` instead) |
| `STRIPE_SECRET_KEY` | The environment's Stripe sandbox key (Secret `stripe`, key `secret-key`) |
| `STRIPE_WEBHOOK_SECRET` | The signing secret `stripe listen` uses (Secret `stripe`, key `webhook-secret`) |
| `PUBLIC_BASE_URL` | Where onboarding returns the seller, e.g. `https://dev.sortedvtg.lan:8443` |
| `RECONCILE_INTERVAL_SECONDS` | Onboarding reconciliation interval (default 300; 0 turns it off) |

`DATABASE_URL` (`postgresql://user:password@pg-rw:5432/payments?sslmode=require`), the Stripe key,
the webhook secret and the public URL are required: the service refuses to start without them.
Alembic migrations (`migrations/`) run at startup under a Postgres advisory lock.

## Routes (contracts/payments.openapi.yaml)

- `POST /connected-accounts/me/onboarding-link`: creates the caller's Accounts v2 account once and returns a fresh Stripe-hosted link (T043).
- `GET /connected-accounts/{account_sub}/payout-status`: `none`, `pending` or `ready`; the seller or Listings only (T043).
- `POST /webhooks/stripe-connect`: signed thin events, stored once and applied by reading the account from Stripe (T044).
- Reconciliation at startup and every 5 minutes for accounts that are not ready (T045).

## Develop

```bash
uv venv --python 3.12 && uv pip install -r requirements-dev.txt
.venv/bin/pytest --cov --cov-report=xml
```

Update pins with `uv pip compile --universal --python-version 3.12 requirements.in -o requirements.txt`
(and the same for `requirements-dev.in`).
