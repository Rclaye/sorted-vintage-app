"""The whole run against fakes of Keycloak, Payments, Listings and the BFF sign-in pages."""

import httpx
import pytest
import respx

from app.config import Seller, Settings
from app.main import run
from app.site import SeedError

KC = "http://kc"
SITE = "https://dev.sortedvtg.lan:8443"
AUTH = "https://auth.sortedvtg.lan:8443/realms/sv-dev"
FORM = (
    f'<form id="kc-form-login" action="{AUTH}/login-actions/authenticate?session_code=a&amp;x=1">'
)
SELL = '<form><input type="hidden" name="csrf_token" value="tok-1"></form>'


def settings(total: int = 4) -> Settings:
    return Settings(
        keycloak_url=KC,
        realm="sv-dev",
        client_id="seed",
        client_secret="s",
        payments_url="http://payments",
        listings_url="http://listings",
        site_url=SITE,
        traefik_host="traefik",
        tls_cert_file="/nope",
        listings_total=total,
        sellers=(
            Seller("demo-seller-1", "demo-seller-1@sortedvtg.lan", "acct_AAAAAAAA1", "pw1"),
            Seller("demo-seller-2", "demo-seller-2@sortedvtg.lan", "acct_AAAAAAAA2", "pw2"),
            Seller("verify-seller", "verify-seller@sortedvtg.lan", "acct_AAAAAAAA3", "pw3"),
        ),
    )


def fake_world(mock: respx.MockRouter, existing_user: str | None = None, published=None):
    published = published or {}
    created = []
    mock.post(f"{KC}/realms/sv-dev/protocol/openid-connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "seed-token"})
    )

    def users(request):
        name = request.url.params["username"]
        return httpx.Response(200, json=[{"id": f"sub-{name}"}] if name == existing_user else [])

    mock.get(f"{KC}/admin/realms/sv-dev/users").mock(side_effect=users)
    mock.put(url__regex=rf"{KC}/admin/realms/sv-dev/users/.*").mock(
        return_value=httpx.Response(204)
    )

    def create_user(request):
        name = httpx.Request("POST", "/", content=request.content).read()
        key = __import__("json").loads(name)["username"]
        created.append(key)
        return httpx.Response(
            201, headers={"location": f"{KC}/admin/realms/sv-dev/users/sub-{key}"}
        )

    mock.post(f"{KC}/admin/realms/sv-dev/users").mock(side_effect=create_user)
    links = mock.put(url__regex=r"http://payments/connected-accounts/.*").mock(
        return_value=httpx.Response(200, json={"payout_status": "ready"})
    )

    def seller_page(request):
        sub = request.url.path.rsplit("/", 1)[-1]
        titles = published.get(sub)
        if titles is None:
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json={"available": [{"title": t} for t in titles], "sold": []})

    mock.get(url__regex=r"http://listings/sellers/.*").mock(side_effect=seller_page)

    mock.get(f"{SITE}/login").mock(
        return_value=httpx.Response(
            303, headers={"location": f"{AUTH}/protocol/openid-connect/auth?x=1"}
        )
    )
    mock.get(f"{AUTH}/protocol/openid-connect/auth").mock(
        return_value=httpx.Response(200, text=FORM)
    )
    login = mock.post(f"{AUTH}/login-actions/authenticate").mock(
        return_value=httpx.Response(302, headers={"location": f"{SITE}/auth/callback?code=c"})
    )
    mock.get(f"{SITE}/auth/callback").mock(
        return_value=httpx.Response(303, headers={"location": "/sell"})
    )
    mock.get(f"{SITE}/sell").mock(return_value=httpx.Response(200, text=SELL))
    counter = iter(range(1, 1000))
    new = mock.post(f"{SITE}/sell/listings").mock(
        side_effect=lambda r: httpx.Response(303, headers={"location": f"/items/L{next(counter)}"})
    )
    photo = mock.post(url__regex=rf"{SITE}/sell/listings/.*/photos").mock(
        side_effect=lambda r: httpx.Response(303, headers={"location": "/items/x"})
    )
    pub = mock.post(url__regex=rf"{SITE}/sell/listings/.*/publish").mock(
        return_value=httpx.Response(303, headers={"location": "/items/x?published=1"})
    )
    return {
        "created": created,
        "links": links,
        "login": login,
        "new": new,
        "photo": photo,
        "pub": pub,
    }


def go(s: Settings, lines: list[str]):
    with httpx.Client() as http:
        return run(s, http, lambda: httpx.Client(), log=lines.append)


@respx.mock
def test_first_run_creates_users_links_and_listings():
    world = fake_world(respx.mock)
    lines: list[str] = []
    created = go(settings(total=4), lines)

    assert world["created"] == ["demo-seller-1", "demo-seller-2", "verify-seller"]
    assert world["links"].call_count == 3
    body = world["links"].calls[0].request.content
    assert b'"stripe_account_id":"acct_AAAAAAAA1"' in body.replace(b" ", b"")
    assert created == {"demo-seller-1": 2, "demo-seller-2": 2}
    assert world["login"].call_count == 2  # the verify seller lists nothing
    assert world["new"].call_count == world["photo"].call_count == world["pub"].call_count == 4
    form = world["new"].calls[0].request.content.decode()
    assert "csrf_token=tok-1" in form and "%2301" in form
    assert any(line.startswith("POST dev.sortedvtg.lan/sell/listings -> 303") for line in lines)


@respx.mock
def test_second_run_only_fills_gaps():
    world = fake_world(
        respx.mock,
        existing_user="demo-seller-1",
        published={
            "sub-demo-seller-1": ["Levi's Western shirt #01", "Wrangler Straight jeans #03"],
            "sub-demo-seller-2": ["Carhartt Chore coat #02"],
        },
    )
    created = go(settings(total=4), [])
    assert "demo-seller-1" not in world["created"]  # updated, not created again
    assert created == {"demo-seller-1": 0, "demo-seller-2": 1}
    assert world["login"].call_count == 1


@respx.mock
def test_a_refused_sign_in_stops_the_run():
    world = fake_world(respx.mock)
    world["login"].mock(return_value=httpx.Response(200, text="Invalid username or password."))
    with pytest.raises(SeedError, match="refused the sign-in"):
        go(settings(total=2), [])


@respx.mock
def test_a_refused_link_stops_the_run():
    world = fake_world(respx.mock)
    world["links"].mock(return_value=httpx.Response(422, json={"error": "unknown_stripe_account"}))
    with pytest.raises(SeedError, match="refused the link"):
        go(settings(), [])


@respx.mock
def test_a_seller_without_an_account_is_created_but_not_linked():
    world = fake_world(respx.mock)
    s = settings(total=2)
    unlinked = Seller("verify-seller", "verify-seller@sortedvtg.lan", "", "pw3")
    s = Settings(**{**s.__dict__, "sellers": (*s.sellers[:2], unlinked)})
    go(s, [])
    assert "verify-seller" in world["created"]
    assert world["links"].call_count == 2
