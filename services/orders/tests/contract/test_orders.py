"""T054: contract tests for Orders (contracts/orders.openapi.yaml), through its HTTP API only.

Listings (http://listings), Payments (http://payments) and Keycloak's token endpoint are answered
by respx; what Orders sends them is part of the contract (Listings gets the buyer's own token,
Payments gets Orders' client-credentials token, research R8).

Contract first (Ticket 3.2): the routes arrive with T061 to T065 (Ticket 3.4), which also adds the
database these fixtures will need. Until then each test is a strict xfail; when a route makes one
pass, CI fails until its marker is removed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import StaticKeys

LISTINGS = "http://listings"
PAYMENTS = "http://payments"
TOKEN_URL = r".*/protocol/openid-connect/token$"


def pending(task: str):
    return pytest.mark.xfail(reason=f"{task} (Ticket 3.4): not built yet", strict=True)


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


@pytest.fixture
def api(settings, private_key, checks, services):
    app = create_app(settings, keys=StaticKeys(private_key.public_key()), checks=checks)
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


@pending("T062")
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


@pending("T062")
def test_the_same_idempotency_key_returns_the_same_order(api, person, happy_path):
    _, buyer = person()
    key = str(uuid.uuid4())
    first = checkout(api, buyer, happy_path["listing_id"], key=key)
    second = checkout(api, buyer, happy_path["listing_id"], key=key)

    assert first.status_code == 201 and second.status_code in (200, 201)
    assert second.json()["id"] == first.json()["id"]
    assert happy_path["reserve"].call_count == 1
    assert happy_path["intent"].call_count == 1


@pending("T062")
def test_an_idempotency_key_is_required(api, person, happy_path):
    _, buyer = person()
    r = api.post("/orders", json={"listing_id": str(happy_path["listing_id"])}, headers=buyer)
    assert r.status_code in (400, 422)
    assert not happy_path["reserve"].called


@pending("T062")
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


@pending("T062")
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


@pending("T062")
def test_the_client_secret_is_never_logged(api, person, happy_path, caplog):
    _, buyer = person()
    r = checkout(api, buyer, happy_path["listing_id"])
    assert r.status_code == 201
    assert "pi_orders_1_secret_test" not in caplog.text


# ---------------------------------------------------------------- GET /orders (T062)


@pending("T062")
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


@pending("T063")
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


@pending("T063")
def test_a_late_success_is_refunded(api, person, services, happy_path, payments_service):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/consume").mock(
        return_value=httpx.Response(409, json={"error": "reservation_released", "message": "x"})
    )
    assert result(api, payments_service, order["id"], "succeeded").json()["status"] == "refunded"


@pending("T063")
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


@pending("T063")
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


@pending("T063")
def test_only_payments_may_post_a_result(api, person, happy_path):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()
    assert result(api, buyer, order["id"], "succeeded").status_code == 403


@pending("T063")
def test_an_unknown_order_is_404(api, person, services, happy_path, payments_service):
    _, buyer = person()
    order = checkout(api, buyer, happy_path["listing_id"]).json()  # the route exists for a real one
    rid = happy_path["reservation"]["id"]
    services.post(f"{LISTINGS}/reservations/{rid}/release").mock(return_value=httpx.Response(200))
    assert result(api, payments_service, order["id"], "failed").status_code == 200
    assert result(api, payments_service, uuid.uuid4(), "succeeded").status_code == 404
