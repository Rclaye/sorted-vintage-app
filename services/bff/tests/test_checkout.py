"""T073: Buy, the pay page, the order page and the buyer's orders, against fake Listings and
Orders (respx), signed in through the same fake Keycloak as test_signin."""

import dataclasses
import json
import logging
import uuid

import httpx
import pytest

from app.checkout import PAY_CSP
from tests.conftest import PUBLIC_BASE
from tests.test_pages import LISTINGS, fake, listing, signed_in, web  # noqa: F401
from tests.test_signin import SUB

ORDERS = "http://orders"
LISTING_ID = "11111111-1111-4111-8111-111111111111"
ORDER_ID = "22222222-2222-4222-8222-222222222222"
CLIENT_SECRET_VALUE = "pi_REPLACE_ME"


@pytest.fixture
def settings(settings):
    return dataclasses.replace(settings, stripe_publishable_key="pk_test_publishable")


def order(**overrides) -> dict:
    base = {
        "id": ORDER_ID,
        "listing_id": LISTING_ID,
        "buyer_sub": SUB,
        "seller_sub": "99999999-9999-4999-8999-999999999999",
        "amount_cents": 4500,
        "status": "pending",
        "failure_reason": None,
        "reservation_expires_at": "2026-11-18T00:10:00Z",
        "created_at": "2026-11-18T00:00:00Z",
        "paid_at": None,
    }
    return {**base, **overrides}


@pytest.fixture
def buyer(web, fake, make_token):  # noqa: F811
    csrf = signed_in(web, fake, make_token)
    fake.get(f"{LISTINGS}/listings/{LISTING_ID}").mock(
        return_value=httpx.Response(200, json=listing())
    )
    return csrf


def buy(web, csrf, key="", listing_id=LISTING_ID):  # noqa: F811
    return web.post(f"/checkout/{listing_id}", data={"csrf_token": csrf, "idempotency_key": key})


# ---------------------------------------------------------------- the Buy button


def test_the_item_page_offers_buy_with_a_fresh_key_each_time(web, fake, buyer):  # noqa: F811
    first = web.get(f"/items/{LISTING_ID}").text
    second = web.get(f"/items/{LISTING_ID}").text

    assert f'action="/checkout/{LISTING_ID}"' in first
    assert "Buy for $45.00" in first
    keys = [p.split('name="idempotency_key" value="')[1].split('"')[0] for p in (first, second)]
    assert all(uuid.UUID(k) for k in keys)
    assert keys[0] != keys[1]


def test_a_visitor_is_asked_to_sign_in_to_buy(web, fake):  # noqa: F811
    fake.get(f"{LISTINGS}/listings/{LISTING_ID}").mock(
        return_value=httpx.Response(200, json=listing())
    )
    page = web.get(f"/items/{LISTING_ID}").text
    assert "Sign in to buy" in page
    assert "/checkout/" not in page


def test_a_seller_gets_no_buy_button_on_their_own_listing(web, fake, buyer):  # noqa: F811
    fake.get(f"{LISTINGS}/listings/{LISTING_ID}").mock(
        return_value=httpx.Response(200, json=listing(seller_sub=SUB))
    )
    assert "/checkout/" not in web.get(f"/items/{LISTING_ID}").text


# ---------------------------------------------------------------- checkout


def test_checkout_starts_an_order_and_shows_the_pay_page(web, fake, buyer, caplog):  # noqa: F811
    key = str(uuid.uuid4())
    orders = fake.post(f"{ORDERS}/orders").mock(
        return_value=httpx.Response(201, json={**order(), "client_secret": CLIENT_SECRET_VALUE})
    )
    caplog.set_level(logging.DEBUG)

    page = buy(web, buyer, key)

    assert page.status_code == 200
    sent = orders.calls[0].request
    assert sent.headers["idempotency-key"] == key
    assert sent.headers["authorization"].startswith("Bearer ")
    assert json.loads(sent.content) == {"listing_id": LISTING_ID}
    assert f'data-client-secret="{CLIENT_SECRET_VALUE}"' in page.text
    assert 'data-publishable-key="pk_test_publishable"' in page.text
    assert f'data-return-url="{PUBLIC_BASE}/orders/{ORDER_ID}"' in page.text
    assert "Pay for Corduroy jacket" in page.text
    assert "https://js.stripe.com/v3/" in page.text
    assert page.headers["content-security-policy"] == PAY_CSP
    assert page.headers["cache-control"] == "no-store"
    assert CLIENT_SECRET_VALUE not in caplog.text


def test_a_double_click_reuses_the_same_key(web, fake, buyer):  # noqa: F811
    key = str(uuid.uuid4())
    orders = fake.post(f"{ORDERS}/orders").mock(
        side_effect=[
            httpx.Response(201, json={**order(), "client_secret": CLIENT_SECRET_VALUE}),
            httpx.Response(200, json={**order(), "client_secret": CLIENT_SECRET_VALUE}),
        ]
    )
    assert buy(web, buyer, key).status_code == 200
    assert buy(web, buyer, key).status_code == 200
    assert {c.request.headers["idempotency-key"] for c in orders.calls} == {key}


def test_a_missing_or_bad_key_gets_a_fresh_one(web, fake, buyer):  # noqa: F811
    orders = fake.post(f"{ORDERS}/orders").mock(
        return_value=httpx.Response(201, json={**order(), "client_secret": CLIENT_SECRET_VALUE})
    )
    buy(web, buyer, "not-a-uuid")
    assert uuid.UUID(orders.calls[0].request.headers["idempotency-key"])


@pytest.mark.parametrize(
    ("status", "error", "shown"),
    [
        (409, "not_available", "just been reserved or sold"),
        (403, "own_listing", "your own listing"),
        (409, "seller_not_ready", "take payments yet"),
        (409, "checkout_in_progress", "still starting"),
        (422, "validation_failed", "missing or not valid"),
        (400, "something_new", "start. Try again"),
    ],
)
def test_a_refused_checkout_goes_back_to_the_item_with_a_message(
    web,  # noqa: F811
    fake,  # noqa: F811
    buyer,
    status,
    error,
    shown,
):
    fake.post(f"{ORDERS}/orders").mock(
        return_value=httpx.Response(status, json={"error": error, "message": "x"})
    )
    response = buy(web, buyer, str(uuid.uuid4()))
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/items/{LISTING_ID}?error=")
    assert shown in web.get(location).text


def test_orders_down_shows_the_friendly_page(web, fake, buyer):  # noqa: F811
    fake.post(f"{ORDERS}/orders").mock(return_value=httpx.Response(503, json={}))
    response = buy(web, buyer, str(uuid.uuid4()))
    assert response.status_code == 503
    assert "Orders" in response.text


def test_a_replayed_order_that_is_over_goes_to_its_page(web, fake, buyer):  # noqa: F811
    fake.post(f"{ORDERS}/orders").mock(return_value=httpx.Response(200, json=order(status="paid")))
    response = buy(web, buyer, str(uuid.uuid4()))
    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{ORDER_ID}"


def test_checkout_needs_the_csrf_token(web, fake, buyer):  # noqa: F811
    orders = fake.post(f"{ORDERS}/orders")
    response = web.post(f"/checkout/{LISTING_ID}", data={"idempotency_key": str(uuid.uuid4())})
    assert response.status_code == 403
    assert not orders.called


def test_checkout_needs_a_signed_in_buyer(web):  # noqa: F811
    response = web.post(f"/checkout/{LISTING_ID}", data={})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/checkout/")


def test_a_bad_listing_id_is_not_found(web, fake, buyer):  # noqa: F811
    assert buy(web, buyer, listing_id="x/../y").status_code == 404


def test_without_a_publishable_key_checkout_is_unavailable(web, fake, buyer):  # noqa: F811
    web.app.state.settings = dataclasses.replace(web.app.state.settings, stripe_publishable_key="")
    orders = fake.post(f"{ORDERS}/orders")
    response = buy(web, buyer, str(uuid.uuid4()))
    assert response.headers["location"] == f"/items/{LISTING_ID}?error=checkout_unavailable"
    assert not orders.called


def test_other_pages_keep_the_strict_csp(web, fake, buyer):  # noqa: F811
    csp = web.get(f"/items/{LISTING_ID}").headers["content-security-policy"]
    assert "stripe.com" not in csp.replace("https://connect.stripe.com", "")
    assert "script-src 'self';" in csp


def test_the_payment_script_is_served_from_the_site(web):  # noqa: F811
    script = web.get("/static/checkout.js")
    assert script.status_code == 200
    assert "confirmPayment" in script.text


# ---------------------------------------------------------------- the order page


def test_a_pending_order_polls_until_it_is_final(web, fake, buyer):  # noqa: F811
    route = fake.get(f"{ORDERS}/orders/{ORDER_ID}")
    route.mock(return_value=httpx.Response(200, json=order()))

    page = web.get(f"/orders/{ORDER_ID}").text
    assert f'hx-get="/orders/{ORDER_ID}"' in page
    assert 'hx-trigger="every 2s"' in page
    assert "Waiting for your payment" in page
    assert "Corduroy jacket" in page

    route.mock(return_value=httpx.Response(200, json=order(status="paid")))
    fragment = web.get(f"/orders/{ORDER_ID}", headers={"HX-Request": "true"}).text
    assert fragment.lstrip().startswith('<div id="order-status"')
    assert "<html" not in fragment
    assert "hx-get" not in fragment
    assert "Paid" in fragment
    assert "confirmation email" in fragment


@pytest.mark.parametrize(
    ("reason", "shown"),
    [
        ("card_declined", "declined"),
        ("expired_card", "expired"),
        ("reservation_expired", "10 minutes"),
        ("some_new_code", "go through"),
        (None, "go through"),
    ],
)
def test_a_failed_order_says_why_in_plain_words(web, fake, buyer, reason, shown):  # noqa: F811
    fake.get(f"{ORDERS}/orders/{ORDER_ID}").mock(
        return_value=httpx.Response(200, json=order(status="failed", failure_reason=reason))
    )
    page = web.get(f"/orders/{ORDER_ID}").text
    assert "Not completed" in page
    assert shown in page
    assert "hx-get" not in page


def test_a_refund_says_so(web, fake, buyer):  # noqa: F811
    fake.get(f"{ORDERS}/orders/{ORDER_ID}").mock(
        return_value=httpx.Response(
            200, json=order(status="refunded", failure_reason="reservation_released")
        )
    )
    page = web.get(f"/orders/{ORDER_ID}").text
    assert "Refunded" in page and "refunded" in page


def test_coming_back_from_a_failed_redirect_says_nothing_was_charged(web, fake, buyer):  # noqa: F811
    fake.get(f"{ORDERS}/orders/{ORDER_ID}").mock(return_value=httpx.Response(200, json=order()))
    page = web.get(f"/orders/{ORDER_ID}?payment_intent=pi_x&redirect_status=failed").text
    assert "Nothing was charged" in page


def test_someone_elses_order_is_not_found(web, fake, buyer):  # noqa: F811
    fake.get(f"{ORDERS}/orders/{ORDER_ID}").mock(return_value=httpx.Response(404, json={}))
    assert web.get(f"/orders/{ORDER_ID}").status_code == 404
    assert web.get("/orders/not-a-uuid").status_code == 404


def test_the_order_page_survives_listings_being_down(web, fake, buyer):  # noqa: F811
    fake.get(f"{LISTINGS}/listings/{LISTING_ID}").mock(return_value=httpx.Response(503))
    fake.get(f"{ORDERS}/orders/{ORDER_ID}").mock(
        return_value=httpx.Response(200, json=order(status="paid"))
    )
    page = web.get(f"/orders/{ORDER_ID}")
    assert page.status_code == 200
    assert "Your item" in page.text


def test_the_order_page_needs_a_signed_in_buyer(web):  # noqa: F811
    response = web.get(f"/orders/{ORDER_ID}")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/orders/")


# ---------------------------------------------------------------- the buyer's orders


def test_the_buyer_sees_their_orders_with_titles(web, fake, buyer):  # noqa: F811
    other = "33333333-3333-4333-8333-333333333333"
    fake.get(f"{ORDERS}/orders").mock(
        return_value=httpx.Response(
            200,
            json=[
                order(status="paid"),
                order(id=other, listing_id=other, status="failed", amount_cents=1200),
            ],
        )
    )
    fake.get(f"{LISTINGS}/listings/{other}").mock(return_value=httpx.Response(404))
    page = web.get("/orders").text
    assert "Corduroy jacket" in page and "$45.00" in page and "Paid" in page
    assert "Your item" in page and "$12.00" in page and "Not completed" in page
    assert 'href="/orders"' in page  # the nav link for signed-in users


def test_no_orders_yet(web, fake, buyer):  # noqa: F811
    fake.get(f"{ORDERS}/orders").mock(return_value=httpx.Response(200, json=[]))
    assert "No orders yet" in web.get("/orders").text
