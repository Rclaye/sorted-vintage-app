"""T054: contract tests for Orders (contracts/orders.openapi.yaml), through its HTTP API only.

Listings (http://listings), Payments (http://payments) and Keycloak's token endpoint are answered
by respx; what Orders sends them is part of the contract (Listings gets the buyer's own token,
Payments gets Orders' client-credentials token, research R8).

Written first (Ticket 3.2) as strict xfails; the routes arrived with T061 to T065 (Ticket 3.4).
They run on a real Postgres (testcontainers), migrated by the app's own lifespan.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import expiry, outbox_relay
from app.db import make_engine
from app.main import create_app
from tests.conftest import StaticKeys

testcontainers = pytest.importorskip("testcontainers.postgres")

LISTINGS = "http://listings"
PAYMENTS = "http://payments"
TOKEN_URL = r".*/protocol/openid-connect/token$"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def services():
    """Listings, Payments and Keycloak as Orders sees them; each test sets the answers it needs."""
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "orders-service-token", "expires_in": 300}
            )
        )
        yield router


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
    """Each test starts with empty tables (the fake Payments always answers pi_orders_1)."""
    engine = make_engine(database_url)
    yield engine
    with engine.begin() as conn:
        if conn.execute(text("select to_regclass('orders')")).scalar():
            conn.execute(text("truncate orders, idempotency_key, outbox_message, payment_result"))
    engine.dispose()


@pytest.fixture
def api(settings, private_key, checks, services, engine):
    """The real app on the test database, with migrations run by its lifespan."""
    app = create_app(
        settings, keys=StaticKeys(private_key.public_key()), checks=checks, engine=engine
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def person(make_token):
    """A signed-in user as the BFF's token presents them."""

    def make():
        sub = uuid.uuid4()
        token = make_token(sub=str(sub), aud=["listings", "orders", "payments"], azp="bff")
        return sub, {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def payments_service(make_token):
    token = make_token(sub=str(uuid.uuid4()), aud=["orders"], azp="payments")
    return {"Authorization": f"Bearer {token}"}


def reservation(listing_id, seller_sub, order_id=None, price=2500) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "listing_id": str(listing_id),
        "order_id": str(order_id or uuid.uuid4()),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "price_cents": price,
        "seller_sub": str(seller_sub),
    }


def checkout(api, buyer_headers, listing_id, key=None):
    headers = {**buyer_headers, "Idempotency-Key": key or str(uuid.uuid4())}
    return api.post("/orders", json={"listing_id": str(listing_id)}, headers=headers)


@pytest.fixture
def happy_path(services, person):
    """A listing that can be reserved and paid for; returns what the tests need to look at."""
    seller_sub, _ = person()
    listing_id = uuid.uuid4()
    held = reservation(listing_id, seller_sub)
    reserve = services.post(f"{LISTINGS}/listings/{listing_id}/reservations").mock(
        return_value=httpx.Response(201, json=held)
    )
    intent = services.post(f"{PAYMENTS}/payment-intents").mock(
        return_value=httpx.Response(
            201,
            json={
                "payment_intent_id": "pi_orders_1",
                "client_secret": "pi_orders_1_secret_test",
                "application_fee_cents": 250,
            },
        )
    )
    return {
        "listing_id": listing_id,
        "seller_sub": seller_sub,
        "reservation": held,
        "reserve": reserve,
        "intent": intent,
    }


# ---------------------------------------------------------------- POST /orders (T062)


def test_checkout_reserves_and_creates_the_payment_intent(api, person, happy_path):
    buyer_sub, buyer = person()
    r = checkout(api, buyer, happy_path["listing_id"])

    assert r.status_code == 201
    order = r.json()
    assert order["status"] == "pending"
    assert order["buyer_sub"] == str(buyer_sub)
    assert order["seller_sub"] == str(happy_path["seller_sub"])
    assert order["listing_id"] == str(happy_path["listing_id"])
    assert order["amount_cents"] == 2500
    assert order["client_secret"] == "pi_orders_1_secret_test"
    assert order["reservation_expires_at"]

    # Listings is called with the buyer's own token, Payments with Orders' service token.
    assert (
        happy_path["reserve"].calls.last.request.headers["Authorization"] == buyer["Authorization"]
    )
    sent = happy_path["intent"].calls.last.request
    assert sent.headers["Authorization"] == "Bearer orders-service-token"
    body = httpx.Request("POST", "/", content=sent.content).read()
    assert b'"amount_cents":2500' in body.replace(b" ", b"")
    assert str(happy_path["seller_sub"]).encode() in body


def test_the_same_idempotency_key_returns_the_same_order(api, person, happy_path):
    _, buyer = person()
    key = str(uuid.uuid4())
    first = checkout(api, buyer, happy_path["listing_id"], key=key)
    second = checkout(api, buyer, happy_path["listing_id"], key=key)

    assert first.status_code == 201 and second.status_code in (200, 201)
    assert second.json()["id"] == first.json()["id"]
    assert happy_path["reserve"].call_count == 1
    assert happy_path["intent"].call_count == 1


def test_an_idempotency_key_is_required(api, person, happy_path):
    _, buyer = person()
    r = api.post("/orders", json={"listing_id": str(happy_path["listing_id"])}, headers=buyer)
    assert r.status_code in (400, 422)
    assert not happy_path["reserve"].called


@pytest.mark.parametrize(("status", "error"), [(403, "own_listing"), (409, "not_available")])
def test_listings_refusals_pass_through(api, person, services, status, error):
    _, buyer = person()
    listing_id = uuid.uuid4()
    services.post(f"{LISTINGS}/listings/{listing_id}/reservations").mock(
        return_value=httpx.Response(status, json={"error": error, "message": "x"})
    )
    intent = services.post(f"{PAYMENTS}/payment-intents")
    r = checkout(api, buyer, listing_id)
    assert r.status_code == status
    assert r.json()["error"] == error
    assert not intent.called


def test_payments_down_releases_the_hold_and_answers_503(api, person, services, happy_path):
    _, buyer = person()
    happy_path["intent"].mock(
        return_value=httpx.Response(503, json={"error": "provider_unavailable", "message": "x"})
    )
    rid = happy_path["reservation"]["id"]
    release = services.post(f"{LISTINGS}/reservations/{rid}/release").mock(
        return_value=httpx.Response(200)
    )
    r = checkout(api, buyer, happy_path["listing_id"])
    assert r.status_code == 503
    assert r.json()["error"] == "payments_unavailable"
    assert release.called


def test_the_client_secret_is_never_logged(api, person, happy_path, caplog):
    _, buyer = person()
    r = checkout(api, buyer, happy_path["listing_id"])
    assert r.status_code == 201
    assert "pi_orders_1_secret_test" not in caplog.text


# ---------------------------------------------------------------- GET /orders (T062)


def test_buyer_and_seller_see_the_order_and_nobody_else_does(api, person, make_token, happy_path):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    seller = {
        "Authorization": "Bearer "
        + make_token(sub=str(happy_path["seller_sub"]), aud=["orders"], azp="bff")
    }
    _, stranger = person()

    assert api.get(f"/orders/{order['id']}", headers=buyer).status_code == 200
    assert api.get(f"/orders/{order['id']}", headers=seller).status_code == 200
    assert api.get(f"/orders/{order['id']}", headers=stranger).status_code == 404
    assert "client_secret" not in api.get(f"/orders/{order['id']}", headers=buyer).json()
    assert [o["id"] for o in api.get("/orders", headers=buyer).json()] == [order["id"]]
    assert api.get("/orders", headers=stranger).json() == []


# ---------------------------------------------------------------- payment-result (T063)


def result(api, headers, order_id, outcome, event_key=None):
    return api.post(
        f"/orders/{order_id}/payment-result",
        json={
            "payment_intent_id": "pi_orders_1",
            "outcome": outcome,
            "event_key": event_key or f"evt_{uuid.uuid4().hex[:12]}",
        },
        headers=headers,
    )


def test_succeeded_consumes_the_reservation_and_the_order_is_paid(
    api, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    consume = services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(
        return_value=httpx.Response(200)
    )

    r = result(api, payments_service, order["id"], "succeeded")

    assert r.status_code == 200
    assert r.json()["status"] == "paid" and r.json()["paid_at"]
    assert consume.calls.last.request.headers["Authorization"] == "Bearer orders-service-token"


def test_a_late_success_is_refunded(api, person, services, happy_path, payments_service):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(
        return_value=httpx.Response(409, json={"error": "reservation_released", "message": "x"})
    )
    assert result(api, payments_service, order["id"], "succeeded").json()["status"] == "refunded"


@pytest.mark.parametrize("outcome", ["failed", "canceled"])
def test_failed_or_canceled_releases_the_hold(
    api, person, services, happy_path, payments_service, outcome
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    release = services.post(f"{LISTINGS}/reservations/{rid}/release").mock(
        return_value=httpx.Response(200)
    )
    r = result(api, payments_service, order["id"], outcome)
    assert r.json()["status"] == "failed"
    assert release.called


def test_a_result_for_a_final_order_is_accepted_and_ignored(
    api, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    consume = services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(
        return_value=httpx.Response(200)
    )
    release = services.post(f"{LISTINGS}/reservations/{rid}/release")
    assert result(api, payments_service, order["id"], "succeeded").json()["status"] == "paid"

    for outcome in ("succeeded", "failed"):
        r = result(api, payments_service, order["id"], outcome)
        assert r.status_code == 200
        assert r.json()["status"] == "paid"
    assert consume.call_count == 1
    assert not release.called


def test_only_payments_may_post_a_result(api, person, happy_path):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    assert result(api, buyer, order["id"], "succeeded").status_code == 403


def test_an_unknown_order_is_404(api, person, services, happy_path, payments_service):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()  # the route exists for a real one
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/release").mock(return_value=httpx.Response(200))
    assert result(api, payments_service, order["id"], "failed").status_code == 200
    assert result(api, payments_service, uuid.uuid4(), "succeeded").status_code == 404


# ---------------------------------------------------------------- beyond the contract: T063-T065
# Expiry, the late-success refund and the outbox, checked against the same database.


def outbox(engine, order_id):
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "select * from outbox_message where payload->'order'->>'id' = :id"
                    " order by created_at"
                ),
                {"id": str(order_id)},
            )
        )


def run_out(engine, order_id):
    """Moves the order's hold expiry into the past."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "update orders set reservation_expires_at = now() - interval '1 minute'"
                " where id = :id"
            ),
            {"id": order_id},
        )


def test_each_state_change_writes_exactly_one_event(
    api, engine, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    assert outbox(engine, order["id"]) == []  # pending writes no event
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(return_value=httpx.Response(200))

    result(api, payments_service, order["id"], "succeeded")
    result(api, payments_service, order["id"], "succeeded")

    events = outbox(engine, order["id"])
    assert [e.event_type for e in events] == ["order.paid"]
    event = events[0]
    assert event.payload["event_id"] == str(event.id)
    assert event.payload["order"]["status"] == "paid"
    assert event.payload["env"] == "test"
    assert event.traceparent and event.published_at is None


def test_listings_down_during_a_result_answers_503_and_changes_nothing(
    api, engine, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(return_value=httpx.Response(503))

    assert result(api, payments_service, order["id"], "succeeded").status_code == 503
    assert api.get(f"/orders/{order['id']}", headers=buyer).json()["status"] == "pending"
    assert outbox(engine, order["id"]) == []


def test_expiry_releases_cancels_and_waits_for_payments(
    api, engine, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    run_out(engine, order["id"])
    rid = happy_path["reservation"]["id"]
    release = services.post(f"{LISTINGS}/reservations/{rid}/release").mock(
        return_value=httpx.Response(200)
    )
    cancel = services.post(f"{PAYMENTS}/payment-intents/pi_orders_1/cancel").mock(
        return_value=httpx.Response(200)
    )
    state = api.app.state

    assert expiry.expire_due(engine, state.listings, state.payments, "test") == 1
    assert release.called and cancel.called
    assert api.get(f"/orders/{order['id']}", headers=buyer).json()["status"] == "pending"
    assert expiry.expire_due(engine, state.listings, state.payments, "test") == 0  # once only

    assert result(api, payments_service, order["id"], "canceled").json()["status"] == "failed"
    assert [e.event_type for e in outbox(engine, order["id"])] == ["order.failed"]


def test_a_payment_that_beats_the_cancel_is_refunded(
    api, engine, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    run_out(engine, order["id"])
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/release").mock(return_value=httpx.Response(200))
    services.post(f"{PAYMENTS}/payment-intents/pi_orders_1/cancel").mock(
        return_value=httpx.Response(200)
    )
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(
        return_value=httpx.Response(409, json={"error": "reservation_released", "message": "x"})
    )
    state = api.app.state
    expiry.expire_due(engine, state.listings, state.payments, "test")

    after = result(api, payments_service, order["id"], "succeeded").json()
    assert after["status"] == "refunded"
    assert after["failure_reason"] == "reservation_released"
    assert [e.event_type for e in outbox(engine, order["id"])] == ["order.refunded"]


def test_payments_down_fails_the_order_and_frees_the_key(api, engine, person, services, happy_path):
    _, buyer = person()
    happy_path["intent"].mock(return_value=httpx.Response(503))
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/release").mock(return_value=httpx.Response(200))
    key = str(uuid.uuid4())
    assert checkout(api, buyer, happy_path["listing_id"], key=key).status_code == 503

    failed = api.get("/orders", headers=buyer).json()
    assert [o["status"] for o in failed] == ["failed"]
    assert [e.event_type for e in outbox(engine, failed[0]["id"])] == ["order.failed"]

    happy_path["intent"].mock(
        return_value=httpx.Response(
            201, json={"payment_intent_id": "pi_orders_2", "client_secret": "pi_orders_2_secret"}
        )
    )
    happy_path["reserve"].mock(
        return_value=httpx.Response(201, json=reservation(happy_path["listing_id"], uuid.uuid4()))
    )
    retry = checkout(api, buyer, happy_path["listing_id"], key=key)
    assert retry.status_code == 201 and retry.json()["id"] != failed[0]["id"]


async def test_the_relay_marks_a_row_only_after_the_confirm_and_keeps_its_trace(
    api, engine, person, services, happy_path, payments_service
):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(return_value=httpx.Response(200))
    result(api, payments_service, order["id"], "succeeded")
    (row,) = outbox(engine, order["id"])

    async def broker_down(message, routing_key):
        raise ConnectionError("no confirm")

    with pytest.raises(ConnectionError):
        await outbox_relay.relay_once(engine, broker_down)
    assert outbox(engine, order["id"])[0].published_at is None

    sent = []

    async def broker(message, routing_key):
        sent.append((message, routing_key))

    while await outbox_relay.relay_once(engine, broker):
        pass
    mine = [(m, k) for m, k in sent if m.message_id == str(row.id)]
    assert len(mine) == 1
    message, routing_key = mine[0]
    assert routing_key == "order.paid"
    assert json.loads(message.body)["order"]["id"] == order["id"]
    # Same trace as the request that wrote the row; a new span (the publish) within it.
    assert message.headers["traceparent"].split("-")[1] == row.traceparent.split("-")[1]
    assert outbox(engine, order["id"])[0].published_at is not None


@pytest.fixture(scope="module")
def amqp_url():
    """RabbitMQ as the lab runs it (the Cluster Operator's 4.3.4), with the T070 exchange."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    try:
        container = (
            DockerContainer("rabbitmq:4.3.4-alpine")
            .with_exposed_ports(5672)
            .waiting_for(LogMessageWaitStrategy("Server startup complete"))
        )
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker is not available: {exc}")
    try:
        host, port = container.get_container_host_ip(), container.get_exposed_port(5672)
        yield f"amqp://guest:guest@{host}:{port}/"
    finally:
        container.stop()


async def test_the_relay_publishes_to_rabbitmq_with_confirms(
    api, engine, amqp_url, person, services, happy_path, payments_service
):
    import aio_pika

    setup = await aio_pika.connect(amqp_url)
    channel = await setup.channel()
    exchange = await channel.declare_exchange("orders", aio_pika.ExchangeType.TOPIC, durable=True)
    queue = await channel.declare_queue("test.orders", exclusive=True)
    await queue.bind(exchange, routing_key="order.*")

    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(return_value=httpx.Response(200))
    result(api, payments_service, order["id"], "succeeded")
    (row,) = outbox(engine, order["id"])

    connection, publish = await outbox_relay.connect(amqp_url)
    try:
        assert await outbox_relay.relay_once(engine, publish) == 1
    finally:
        await connection.close()

    message = await queue.get(timeout=5)
    await message.ack()
    assert message.routing_key == "order.paid"
    assert message.message_id == str(row.id)
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert json.loads(message.body)["event_id"] == str(row.id)
    assert message.headers["traceparent"].split("-")[1] == row.traceparent.split("-")[1]
    assert outbox(engine, order["id"])[0].published_at is not None
    await setup.close()


def test_checkout_passes_the_order_on_to_listings_and_payments(api, person, happy_path):
    """T083: the order id travels as W3C baggage to the services Orders calls."""
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()

    for route in (happy_path["reserve"], happy_path["intent"]):
        assert f"order_id={order['id']}" in route.calls.last.request.headers["baggage"]
