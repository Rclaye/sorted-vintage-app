"""T048: the US1 pages against fake Listings and Payments (respx), signed in through the same
fake Keycloak as test_signin."""

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from tests.test_signin import SUB, sign_in

LISTINGS = "http://listings"
PAYMENTS = "http://payments"
STRIPE_LINK = "https://connect.stripe.com/setup/c/acct_test/abc"


def listing(**overrides) -> dict:
    base = {
        "id": "11111111-1111-4111-8111-111111111111",
        "seller_sub": "99999999-9999-4999-8999-999999999999",
        "seller_display_name": "Jo",
        "title": "Corduroy jacket",
        "brand": "Levi's",
        "category": "outerwear",
        "size": "M",
        "condition": "good",
        "price_cents": 4500,
        "description": "Warm & sturdy <b>really</b>",
        "status": "available",
        "version": 2,
        "photos": [
            {
                "id": "p1",
                "url": "/photos/listings/x/p1",
                "content_type": "image/jpeg",
                "position": 0,
            }
        ],
        "published_at": "2026-11-11T00:00:00Z",
    }
    return {**base, **overrides}


@pytest.fixture
def fake():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def web(client):
    client.follow_redirects = False
    return client


def signed_in(web, fake, make_token) -> str:
    sign_in(web, fake, make_token)
    fake.get(f"{LISTINGS}/sellers/{SUB}").mock(
        return_value=httpx.Response(
            200, json={"display_name": "Sam V.", "drafts": [], "available": [], "sold": []}
        )
    )
    fake.get(f"{PAYMENTS}/connected-accounts/{SUB}/payout-status").mock(
        return_value=httpx.Response(200, json={"payout_status": "none"})
    )
    page = web.get("/sell").text
    return page.split('name="csrf_token" value="')[1].split('"')[0]


# ---------------------------------------------------------------- browse and search


def test_home_shows_available_listings_and_escapes_text(web, fake):
    fake.get(f"{LISTINGS}/listings").mock(
        return_value=httpx.Response(
            200, json={"items": [listing(title="<script>x</script>")], "next_cursor": None}
        )
    )
    page = web.get("/")
    assert page.status_code == 200
    assert "&lt;script&gt;x&lt;/script&gt;" in page.text and "<script>x</script>" not in page.text
    assert "$45.00" in page.text
    assert '<script src="/static/htmx-2.0.11.min.js"' in page.text


def test_search_passes_filters_and_turns_dollars_into_cents(web, fake):
    route = fake.get(f"{LISTINGS}/listings").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )
    web.get("/search?q=velvet&brand=Levi%27s&size=M&category=outerwear&min=10&max=99.5&category2=x")
    sent = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert sent["q"] == ["velvet"] and sent["brand"] == ["Levi's"] and sent["size"] == ["M"]
    assert sent["category"] == ["outerwear"]
    assert sent["min_price_cents"] == ["1000"] and sent["max_price_cents"] == ["9950"]


def test_htmx_search_returns_only_the_results(web, fake):
    fake.get(f"{LISTINGS}/listings").mock(
        return_value=httpx.Response(200, json={"items": [listing()], "next_cursor": "abc"})
    )
    part = web.get("/search?q=jacket", headers={"HX-Request": "true"})
    assert "<html" not in part.text and "Corduroy jacket" in part.text
    assert "cursor=abc" in part.text


# ---------------------------------------------------------------- a listing, a seller


def test_an_unknown_listing_is_a_404_page(web, fake):
    fake.get(url__regex=rf"{LISTINGS}/listings/.+").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "message": "x"})
    )
    page = web.get("/items/nope")
    assert page.status_code == 404 and "Not found" in page.text


def test_a_visitor_sees_no_seller_controls(web, fake):
    fake.get(f"{LISTINGS}/listings/{listing()['id']}").mock(
        return_value=httpx.Response(200, json=listing())
    )
    page = web.get(f"/items/{listing()['id']}").text
    assert "Corduroy jacket" in page and "Publish" not in page and "Upload" not in page


def test_the_seller_sees_upload_and_publish_on_a_draft(web, fake, make_token):
    signed_in(web, fake, make_token)
    draft = listing(seller_sub=SUB, status="draft", photos=[])
    route = fake.get(f"{LISTINGS}/listings/{draft['id']}").mock(
        return_value=httpx.Response(200, json=draft)
    )
    page = web.get(f"/items/{draft['id']}").text
    assert "Upload" in page and "Publish" in page
    assert route.calls.last.request.headers["authorization"].startswith("Bearer ")


def test_a_seller_page(web, fake):
    fake.get(f"{LISTINGS}/sellers/{listing()['seller_sub']}").mock(
        return_value=httpx.Response(
            200, json={"display_name": "Jo", "sold_count": 3, "available": [listing()], "sold": []}
        )
    )
    page = web.get(f"/sellers/{listing()['seller_sub']}").text
    assert "Jo" in page and "3 sold" in page and "Corduroy jacket" in page


# ---------------------------------------------------------------- selling


def test_sell_needs_sign_in(web):
    response = web.get("/sell")
    assert response.status_code == 303 and response.headers["location"] == "/login?next=/sell"


def test_sell_shows_drafts_and_payout_status(web, fake, make_token):
    sign_in(web, fake, make_token)
    fake.get(f"{LISTINGS}/sellers/{SUB}").mock(
        return_value=httpx.Response(
            200,
            json={
                "display_name": "Sam",
                "drafts": [listing(title="My draft", status="draft")],
                "available": [],
                "sold": [],
            },
        )
    )
    fake.get(f"{PAYMENTS}/connected-accounts/{SUB}/payout-status").mock(
        return_value=httpx.Response(200, json={"payout_status": "pending"})
    )
    page = web.get("/sell").text
    assert "My draft" in page and "Set up payouts with Stripe" in page


def test_creating_a_draft_needs_the_csrf_token(web, fake, make_token):
    signed_in(web, fake, make_token)
    create = fake.post(f"{LISTINGS}/listings").mock(
        return_value=httpx.Response(201, json=listing())
    )
    assert web.post("/sell/listings", data={"title": "x", "csrf_token": "wrong"}).status_code == 403
    assert create.call_count == 0


def test_creating_a_draft_goes_to_its_page(web, fake, make_token):
    csrf = signed_in(web, fake, make_token)
    create = fake.post(f"{LISTINGS}/listings").mock(
        return_value=httpx.Response(201, json=listing(status="draft"))
    )
    form = {
        "csrf_token": csrf,
        "title": "Corduroy jacket",
        "brand": "Levi's",
        "category": "outerwear",
        "size": "M",
        "condition": "good",
        "price": "12.50",
        "description": "",
    }
    response = web.post("/sell/listings", data=form)
    assert (
        response.status_code == 303 and response.headers["location"] == f"/items/{listing()['id']}"
    )
    sent = json.loads(create.calls.last.request.content)
    assert sent["price_cents"] == 1250 and sent["title"] == "Corduroy jacket"


def test_a_rejected_photo_comes_back_as_a_message(web, fake, make_token):
    csrf = signed_in(web, fake, make_token)
    lid = listing()["id"]
    upload = fake.post(f"{LISTINGS}/listings/{lid}/photos").mock(
        return_value=httpx.Response(415, json={"error": "photo_type_rejected", "message": "x"})
    )
    response = web.post(
        f"/sell/listings/{lid}/photos",
        data={"csrf_token": csrf},
        files={"file": ("a.jpg", b"not an image", "image/jpeg")},
    )
    assert response.headers["location"] == f"/items/{lid}?error=photo_type_rejected"
    assert b"not an image" in upload.calls.last.request.content
    fake.get(f"{LISTINGS}/listings/{lid}").mock(return_value=httpx.Response(200, json=listing()))
    assert "JPEG, PNG or WebP" in web.get(f"/items/{lid}?error=photo_type_rejected").text


def test_publishing_without_payouts_goes_to_onboarding(web, fake, make_token):
    csrf = signed_in(web, fake, make_token)
    lid = listing()["id"]
    fake.post(f"{LISTINGS}/listings/{lid}/publish").mock(
        return_value=httpx.Response(
            409, json={"error": "payout_onboarding_required", "message": "x"}
        )
    )
    response = web.post(f"/sell/listings/{lid}/publish", data={"csrf_token": csrf})
    assert response.headers["location"] == f"/sell/onboarding?next=%2Fitems%2F{lid}"


def test_publishing_goes_to_the_published_listing(web, fake, make_token):
    csrf = signed_in(web, fake, make_token)
    lid = listing()["id"]
    fake.post(f"{LISTINGS}/listings/{lid}/publish").mock(
        return_value=httpx.Response(200, json=listing())
    )
    response = web.post(f"/sell/listings/{lid}/publish", data={"csrf_token": csrf})
    assert response.headers["location"] == f"/items/{lid}?published=1"


def test_onboarding_sends_a_seller_who_is_not_ready_to_stripe(web, fake, make_token):
    signed_in(web, fake, make_token)
    fake.post(f"{PAYMENTS}/connected-accounts/me/onboarding-link").mock(
        return_value=httpx.Response(200, json={"url": STRIPE_LINK, "expires_at": "x"})
    )
    response = web.get("/sell/onboarding")
    assert response.status_code == 303 and response.headers["location"] == STRIPE_LINK


def test_coming_back_ready_from_stripe_returns_to_the_listing(web, fake, make_token):
    signed_in(web, fake, make_token)
    fake.get(f"{PAYMENTS}/connected-accounts/{SUB}/payout-status").mock(
        return_value=httpx.Response(200, json={"payout_status": "ready"})
    )
    assert web.get("/sell/onboarding?next=/items/x").headers["location"] == "/items/x?payouts=ready"
    assert web.get("/sell/onboarding").headers["location"] == "/sell?payouts=ready"
    assert (
        web.get("/sell/onboarding?next=https://evil.example").headers["location"]
        == "/?payouts=ready"
    )


# ---------------------------------------------------------------- photos, failures, headers


A, B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def test_photos_are_forwarded_from_listings(web, fake):
    fake.get(f"{LISTINGS}/photos/listings/{A}/{B}").mock(
        return_value=httpx.Response(
            200,
            content=b"\xff\xd8\xffjpeg",
            headers={
                "content-type": "image/jpeg",
                "cache-control": "max-age=31536000, public, immutable",
            },
        )
    )
    response = web.get(f"/photos/listings/{A}/{B}")
    assert response.status_code == 200 and response.content == b"\xff\xd8\xffjpeg"
    assert response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]


@pytest.mark.parametrize(
    "path",
    [
        "/photos/../secrets",
        "/photos/listings/a/b",
        f"/photos/listings/{A}/..%2F..%2Fhealthz",
        f"/photos/other/{A}/{B}",
        f"/photos/listings/{A}/{B}/extra",
    ],
)
def test_only_listing_and_photo_ids_reach_listings(web, fake, path):
    upstream = fake.get(url__regex=r".*").mock(return_value=httpx.Response(200, content=b"x"))
    assert web.get(path).status_code == 404
    assert upstream.call_count == 0


def test_a_service_that_is_down_shows_a_friendly_503(web, fake):
    fake.get(f"{LISTINGS}/listings").mock(side_effect=httpx.ConnectError("down"))
    page = web.get("/")
    assert page.status_code == 503 and "Listings isn't answering" in page.text


def test_pages_carry_security_headers(web, fake):
    fake.get(f"{LISTINGS}/listings").mock(return_value=httpx.Response(200, json={"items": []}))
    headers = web.get("/").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "https://connect.stripe.com" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


def test_the_vendored_htmx_is_the_reviewed_file(web):
    static = Path(__file__).parent.parent / "app" / "static" / "htmx-2.0.11.min.js"
    assert hashlib.sha256(static.read_bytes()).hexdigest() == (
        "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
    )
    assert web.get("/static/htmx-2.0.11.min.js").status_code == 200
