"""T055: contract tests for the purchase side of Payments (contracts/payments.openapi.yaml):
POST /payment-intents, POST /payment-intents/{id}/cancel, POST /webhooks/stripe and
GET /orders/{order_id}/ledger, on a real Postgres.

Stripe is answered by respx, as in test_connect.py, rather than stripe-mock: the fakes check the
request Payments sends, and the Sprint 3 spike (sprints/sprint-3/spikes.md) recorded what the
sandbox really accepts and returns. Ticket 3.5 still makes one live sandbox call before it closes.

Written first (Ticket 3.2) as strict xfails; the routes arrived with T066 to T068 (Ticket 3.5).
The second half goes beyond the contract: the inbox worker against a fake Orders, refunds, and
payment reconciliation (T067 to T069).
"""

import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import inbox_worker
from app.db import make_engine, payment_event
from app.ledger import entries_for
from app.reconcile import reconcile_payments
from tests.contract.test_connect import STRIPE, make_api, signed, store_account

testcontainers = pytest.importorskip("testcontainers.postgres")


SELLER_ACCOUNT = "acct_1UaNnWKmlbGfFXXi"  # the shape of a real test account; never called


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def database_url():
    try:
        container = testcontainers.PostgresContainer("postgres:17-alpine", driver=None)
        container.start()
    except Exception as exc:  # no Docker on this machine; CI and the lab host have it
        pytest.skip(f"Docker is not available: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def engine(database_url):
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def stripe_api():
    import respx

    with respx.mock(base_url=STRIPE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def api(settings, private_key, engine, stripe_api):
    with TestClient(make_api(settings, private_key, engine), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def orders_service(make_token):
    """Orders' client-credentials token: azp orders, payments in aud (realm file)."""
    token = make_token(sub=str(uuid.uuid4()), aud=["payments", "listings"], azp="orders")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def buyer(make_token):
    token = make_token(sub=str(uuid.uuid4()), aud=["listings", "orders", "payments"], azp="bff")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def ready_seller(engine):
    sub = uuid.uuid4()
    account = f"acct_{uuid.uuid4().hex[:16]}"
    store_account(engine, sub, account, card="active", transfers="active")
    return sub, account


def payment_intent_json(pi_id: str, status: str = "requires_payment_method", **extra) -> dict:
    return {
        "id": pi_id,
        "object": "payment_intent",
        "amount": 2500,
        "currency": "usd",
        "status": status,
        "client_secret": f"{pi_id}_secret_test",
        "application_fee_amount": 250,
        "transfer_data": {"destination": SELLER_ACCOUNT},
        **extra,
    }


def body_of(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def order_request(seller_sub, order_id=None, amount=2500, fee_pct=10) -> dict:
    return {
        "order_id": str(order_id or uuid.uuid4()),
        "amount_cents": amount,
        "fee_pct": fee_pct,
        "seller_sub": str(seller_sub),
    }


def platform_event(event_type: str, pi_id: str, event_id: str | None = None) -> bytes:
    return json.dumps(
        {
            "id": event_id or f"evt_{uuid.uuid4().hex[:20]}",
            "object": "event",
            "type": event_type,
            "data": {"object": payment_intent_json(pi_id, status=event_type.split(".")[-1])},
        }
    ).encode()


def inbox_count(engine, **where) -> int:
    query = select(func.count()).select_from(payment_event)
    for column, value in where.items():
        query = query.where(payment_event.c[column] == value)
    with engine.connect() as conn:
        return conn.execute(query).scalar_one()


# ---------------------------------------------------------------- POST /payment-intents (T066)


def test_a_destination_charge_for_a_ready_seller(api, stripe_api, orders_service, ready_seller):
    seller_sub, account = ready_seller
    route = stripe_api.post("/v1/payment_intents").mock(
        return_value=httpx.Response(200, json=payment_intent_json("pi_contract_1"))
    )
    body = order_request(seller_sub, amount=2500, fee_pct=10)
    r = api.post("/payment-intents", json=body, headers=orders_service)

    assert r.status_code == 201
    assert r.json() == {
        "payment_intent_id": "pi_contract_1",
        "client_secret": "pi_contract_1_secret_test",
        "application_fee_cents": 250,
    }
    sent = route.calls.last.request
    form = body_of(sent)
    assert form["amount"] == "2500" and form["currency"] == "usd"
    assert form["application_fee_amount"] == "250"
    assert form["transfer_data[destination]"] == account
    assert form["automatic_payment_methods[enabled]"] == "true"
    assert not any(k.startswith("payment_method_types") for k in form)  # refused since T009
    assert sent.headers["Idempotency-Key"] == body["order_id"]


def test_the_fee_rounds_half_up(api, stripe_api, orders_service, ready_seller):
    route = stripe_api.post("/v1/payment_intents").mock(
        return_value=httpx.Response(200, json=payment_intent_json("pi_contract_2"))
    )
    r = api.post(
        "/payment-intents",
        json=order_request(ready_seller[0], amount=105, fee_pct=10),
        headers=orders_service,
    )
    assert r.status_code == 201
    assert body_of(route.calls.last.request)["application_fee_amount"] == "11"


def test_a_seller_without_a_ready_account_is_refused(api, stripe_api, orders_service, engine):
    not_ready = uuid.uuid4()
    store_account(engine, not_ready, f"acct_{uuid.uuid4().hex[:16]}")
    route = stripe_api.post("/v1/payment_intents")
    for seller_sub in (not_ready, uuid.uuid4()):
        r = api.post("/payment-intents", json=order_request(seller_sub), headers=orders_service)
        assert r.status_code == 409
        assert r.json()["error"] == "seller_not_ready"
    assert not route.called


def test_stripe_unreachable_is_503(api, stripe_api, orders_service, ready_seller):
    stripe_api.post("/v1/payment_intents").mock(side_effect=httpx.ConnectError("down"))
    r = api.post("/payment-intents", json=order_request(ready_seller[0]), headers=orders_service)
    assert r.status_code == 503
    assert r.json()["error"] == "provider_unavailable"


def test_only_orders_may_create_payment_intents(api, stripe_api, buyer, ready_seller):
    route = stripe_api.post("/v1/payment_intents")
    r = api.post("/payment-intents", json=order_request(ready_seller[0]), headers=buyer)
    assert r.status_code == 403
    assert not route.called


def test_the_client_secret_is_never_logged(api, stripe_api, orders_service, ready_seller, caplog):
    stripe_api.post("/v1/payment_intents").mock(
        return_value=httpx.Response(200, json=payment_intent_json("pi_contract_3"))
    )
    r = api.post("/payment-intents", json=order_request(ready_seller[0]), headers=orders_service)
    assert r.status_code == 201  # otherwise the check below passes for want of a route
    assert r.json()["client_secret"] == "pi_contract_3_secret_test"
    assert "pi_contract_3_secret_test" not in caplog.text


# ---------------------------------------------------------------- cancel (T066)


def test_cancel_is_idempotent(api, stripe_api, orders_service):
    stripe_api.post("/v1/payment_intents/pi_contract_4/cancel").mock(
        side_effect=[
            httpx.Response(200, json=payment_intent_json("pi_contract_4", status="canceled")),
            httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "code": "payment_intent_unexpected_state",
                        "message": "This PaymentIntent's status is canceled.",
                    }
                },
            ),
        ]
    )
    for _ in range(2):
        r = api.post("/payment-intents/pi_contract_4/cancel", headers=orders_service)
        assert r.status_code == 200


# ---------------------------------------------------------------- POST /webhooks/stripe (T067)


def test_a_signed_platform_event_is_stored_in_the_inbox(api, engine):
    payload = platform_event("payment_intent.succeeded", "pi_contract_5", "evt_contract_5")
    r = api.post("/webhooks/stripe", content=payload, headers=signed(payload))
    assert r.status_code == 200
    assert inbox_count(engine, stripe_event_id="evt_contract_5", source="webhook") == 1
    assert inbox_count(engine, payment_intent_id="pi_contract_5") == 1


def test_a_replayed_platform_event_is_stored_once(api, engine):
    payload = platform_event("payment_intent.payment_failed", "pi_contract_6", "evt_contract_6")
    for _ in range(2):
        assert (
            api.post("/webhooks/stripe", content=payload, headers=signed(payload)).status_code
            == 200
        )
    assert inbox_count(engine, stripe_event_id="evt_contract_6") == 1


def test_a_bad_signature_stores_nothing(api, engine):
    payload = platform_event("payment_intent.succeeded", "pi_contract_7", "evt_contract_7")
    for headers in (
        signed(payload, secret="whsec_wrong"),
        {"Stripe-Signature": "t=1,v1=00", "Content-Type": "application/json"},
        {"Content-Type": "application/json"},
    ):
        assert api.post("/webhooks/stripe", content=payload, headers=headers).status_code == 400
    assert inbox_count(engine, stripe_event_id="evt_contract_7") == 0


# ---------------------------------------------------------------- GET /orders/{id}/ledger (T068)


def test_the_ledger_of_a_paid_order_sums_to_the_amount(api, engine, orders_service, ready_seller):
    from app.ledger import record_sale

    order_id = uuid.uuid4()
    record_sale(
        engine, order_id=order_id, seller_sub=ready_seller[0], amount_cents=2500, fee_pct=10
    )
    r = api.get(f"/orders/{order_id}/ledger", headers=orders_service)
    assert r.status_code == 200
    entries = r.json()
    assert {(e["party"], e["kind"], e["amount_cents"]) for e in entries} == {
        ("platform", "sale", 250),
        ("seller", "sale", 2250),
    }
    assert sum(e["amount_cents"] for e in entries) == 2500
    seller_rows = [e for e in entries if e["party"] == "seller"]
    assert seller_rows[0]["account_sub"] == str(ready_seller[0])
    assert [e["account_sub"] for e in entries if e["party"] == "platform"] == [None]


def test_a_sale_is_recorded_once(engine, ready_seller):
    from app.ledger import entries_for, record_sale

    order_id = uuid.uuid4()
    for _ in range(2):
        record_sale(
            engine, order_id=order_id, seller_sub=ready_seller[0], amount_cents=999, fee_pct=12.5
        )
    assert len(entries_for(engine, order_id)) == 2


# ---------------------------------------------------------------- beyond the contract: T067-T069
# The inbox worker against a fake Orders, the refund path and payment reconciliation.

ORDERS = "http://orders"


@pytest.fixture
def orders_api():
    """Orders and Keycloak's token endpoint as the worker sees them."""
    import respx

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=r".*/protocol/openid-connect/token$").mock(
            return_value=httpx.Response(
                200, json={"access_token": "payments-service-token", "expires_in": 300}
            )
        )
        yield router


def order_says(orders_api, order_id, status: str):
    return orders_api.post(f"{ORDERS}/orders/{order_id}/payment-result").mock(
        return_value=httpx.Response(200, json={"id": str(order_id), "status": status})
    )


@pytest.fixture
def charge(api, stripe_api, orders_service, ready_seller):
    """A PaymentIntent created through the API for a new order; returns (order_id, pi_id)."""

    def make(amount=2500, fee_pct=10):
        order_id, pi_id = uuid.uuid4(), f"pi_{uuid.uuid4().hex[:16]}"
        stripe_api.post("/v1/payment_intents").mock(
            return_value=httpx.Response(200, json=payment_intent_json(pi_id))
        )
        r = api.post(
            "/payment-intents",
            json=order_request(ready_seller[0], order_id, amount, fee_pct),
            headers=orders_service,
        )
        assert r.status_code == 201
        return order_id, pi_id

    return make


def deliver(api, event_type: str, pi_id: str) -> str:
    event_id = f"evt_{uuid.uuid4().hex[:20]}"
    payload = platform_event(event_type, pi_id, event_id)
    assert api.post("/webhooks/stripe", content=payload, headers=signed(payload)).status_code == 200
    return event_id


def work(api, engine) -> int:
    return inbox_worker.run_once(engine, api.app.state.orders, api.app.state.charges)


def event_row(engine, event_id):
    with engine.connect() as conn:
        return conn.execute(
            select(payment_event).where(payment_event.c.stripe_event_id == event_id)
        ).one()


def ledger_of(engine, order_id) -> set:
    return {(e.party, e.kind, e.amount_cents) for e in entries_for(engine, order_id)}


def test_a_success_orders_calls_paid_writes_the_sale(api, engine, orders_api, charge):
    order_id, pi_id = charge()
    said = order_says(orders_api, order_id, "paid")
    event_id = deliver(api, "payment_intent.succeeded", pi_id)

    assert work(api, engine) >= 1
    sent = said.calls.last.request
    assert sent.headers["Authorization"] == "Bearer payments-service-token"
    assert json.loads(sent.content) == {
        "payment_intent_id": pi_id,
        "outcome": "succeeded",
        "event_key": event_id,
    }
    assert ledger_of(engine, order_id) == {("platform", "sale", 250), ("seller", "sale", 2250)}
    assert event_row(engine, event_id).applied_at is not None


@pytest.mark.parametrize("status", ["refunded", "failed"])
def test_a_success_orders_cannot_honour_is_refunded_in_full(
    api, engine, stripe_api, orders_api, charge, status
):
    order_id, pi_id = charge()
    order_says(orders_api, order_id, status)
    refund = stripe_api.post("/v1/refunds").mock(
        return_value=httpx.Response(
            200, json={"id": "re_1", "object": "refund", "status": "succeeded"}
        )
    )
    deliver(api, "payment_intent.succeeded", pi_id)
    work(api, engine)

    form = body_of(refund.calls.last.request)
    assert form["payment_intent"] == pi_id
    assert form["reverse_transfer"] == "true" and form["refund_application_fee"] == "true"
    assert refund.calls.last.request.headers["Idempotency-Key"] == f"refund-{order_id}"
    entries = entries_for(engine, order_id)
    assert len(entries) == 4 and sum(e.amount_cents for e in entries) == 0


def test_a_declined_payment_fails_the_order_and_cancels_the_intent(
    api, engine, stripe_api, orders_api, charge
):
    order_id, pi_id = charge()
    said = order_says(orders_api, order_id, "failed")
    cancel = stripe_api.post(f"/v1/payment_intents/{pi_id}/cancel").mock(
        return_value=httpx.Response(200, json=payment_intent_json(pi_id, status="canceled"))
    )
    deliver(api, "payment_intent.payment_failed", pi_id)
    work(api, engine)

    assert json.loads(said.calls.last.request.content)["outcome"] == "failed"
    assert cancel.called
    assert ledger_of(engine, order_id) == set()


@pytest.mark.parametrize("answer", [404, 503])
def test_an_event_orders_cannot_take_yet_is_kept_and_retried(
    api, engine, orders_api, charge, answer
):
    order_id, pi_id = charge()
    route = orders_api.post(f"{ORDERS}/orders/{order_id}/payment-result").mock(
        return_value=httpx.Response(answer, json={"error": "x", "message": "x"})
    )
    event_id = deliver(api, "payment_intent.succeeded", pi_id)
    work(api, engine)

    row = event_row(engine, event_id)
    assert row.applied_at is None and row.attempts == 1 and row.next_attempt_at is not None
    work(api, engine)  # not due yet: backoff
    assert route.call_count == 1

    order_says(orders_api, order_id, "paid")
    with engine.begin() as conn:
        conn.execute(
            payment_event.update()
            .where(payment_event.c.stripe_event_id == event_id)
            .values(next_attempt_at=None)
        )
    work(api, engine)
    assert event_row(engine, event_id).applied_at is not None
    assert len(entries_for(engine, order_id)) == 2


def test_the_same_outcome_twice_writes_the_sale_once(api, engine, orders_api, charge):
    order_id, pi_id = charge()
    order_says(orders_api, order_id, "paid")
    for _ in range(2):
        deliver(api, "payment_intent.succeeded", pi_id)
    work(api, engine)
    assert len(entries_for(engine, order_id)) == 2


def test_an_event_for_a_payment_intent_payments_did_not_create_is_set_aside(
    api, engine, orders_api
):
    route = orders_api.post(url__regex=rf"{ORDERS}/orders/.*")
    event_id = deliver(api, "payment_intent.succeeded", f"pi_{uuid.uuid4().hex[:16]}")
    work(api, engine)
    assert event_row(engine, event_id).applied_at is not None
    assert not route.called


def test_reconciliation_finds_a_payment_the_forwarder_missed(
    api, engine, stripe_api, orders_api, charge
):
    order_id, pi_id = charge()
    order_says(orders_api, order_id, "paid")
    stripe_api.get(f"/v1/payment_intents/{pi_id}").mock(
        return_value=httpx.Response(200, json=payment_intent_json(pi_id, status="succeeded"))
    )
    charges = api.app.state.charges

    assert reconcile_payments(engine, charges, pending_after_seconds=3600) == 0  # too recent
    assert reconcile_payments(engine, charges, pending_after_seconds=0) >= 1
    key = f"recon:{pi_id}:succeeded"
    assert inbox_count(engine, stripe_event_id=key, source="reconciliation") == 1
    reconcile_payments(engine, charges, pending_after_seconds=0)
    assert inbox_count(engine, stripe_event_id=key) == 1  # the key deduplicates

    work(api, engine)
    assert ledger_of(engine, order_id) == {("platform", "sale", 250), ("seller", "sale", 2250)}
    # Settled: reconciliation no longer asks Stripe about it.
    stripe_api.get(f"/v1/payment_intents/{pi_id}").mock(side_effect=AssertionError("asked"))
    reconcile_payments(engine, charges, pending_after_seconds=0)


def test_the_seller_reads_their_ledger_and_a_stranger_does_not(
    api, engine, orders_api, charge, ready_seller, make_token, buyer
):
    order_id, pi_id = charge()
    order_says(orders_api, order_id, "paid")
    deliver(api, "payment_intent.succeeded", pi_id)
    work(api, engine)
    seller = {
        "Authorization": "Bearer " + make_token(sub=str(ready_seller[0]), aud="payments", azp="bff")
    }
    assert len(api.get(f"/orders/{order_id}/ledger", headers=seller).json()) == 2
    assert api.get(f"/orders/{order_id}/ledger", headers=buyer).status_code == 404


def test_the_worker_tells_orders_which_order_it_is_reporting(api, engine, orders_api, charge):
    """T083: the inbox worker's call to Orders carries the order id as W3C baggage."""
    order_id, pi_id = charge()
    said = order_says(orders_api, order_id, "paid")
    deliver(api, "payment_intent.succeeded", pi_id)
    work(api, engine)
    assert f"order_id={order_id}" in said.calls.last.request.headers["baggage"]
