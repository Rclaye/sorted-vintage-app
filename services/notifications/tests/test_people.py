"""Addresses from Keycloak with Notifications' own token; titles from Listings, best effort."""

import httpx
import pytest
import respx

from app.people import AddressError, KeycloakUsers, ListingTitles, ServiceToken

TOKEN_URL = "http://keycloak.keycloak/realms/sv-dev/protocol/openid-connect/token"
ADMIN = "http://keycloak.keycloak/admin/realms/sv-dev"
SUB = "4f8d6a8e-2b1c-4d5e-9f00-0123456789ab"


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def users(http):
    return KeycloakUsers(http, ADMIN, ServiceToken(http, TOKEN_URL, "notifications", "s3cret"))


@respx.mock
async def test_the_email_is_read_with_a_client_credentials_token_fetched_once(users):
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "t-1", "expires_in": 300})
    )
    user = respx.get(f"{ADMIN}/users/{SUB}").mock(
        return_value=httpx.Response(200, json={"id": SUB, "email": "buyer@sortedvtg.lan"})
    )

    assert await users.email_of(SUB) == "buyer@sortedvtg.lan"
    assert await users.email_of(SUB) == "buyer@sortedvtg.lan"

    assert token.call_count == 1
    sent = token.calls[0].request
    assert b"grant_type=client_credentials" in sent.content
    assert sent.headers["authorization"].startswith("Basic ")
    assert user.calls[0].request.headers["authorization"] == "Bearer t-1"


@respx.mock
async def test_an_expiring_token_is_fetched_again(users):
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 10})
    )
    respx.get(f"{ADMIN}/users/{SUB}").mock(
        return_value=httpx.Response(200, json={"email": "a@sortedvtg.lan"})
    )
    await users.email_of(SUB)
    await users.email_of(SUB)
    assert token.call_count == 2


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404),
        httpx.Response(403),
        httpx.Response(200, json={"id": SUB}),
        httpx.ConnectError("refused"),
    ],
)
async def test_no_address_is_an_address_error(users, answer):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    route = respx.get(f"{ADMIN}/users/{SUB}")
    if isinstance(answer, Exception):
        route.mock(side_effect=answer)
    else:
        route.mock(return_value=answer)
    with pytest.raises(AddressError):
        await users.email_of(SUB)


@respx.mock
async def test_a_refused_token_is_an_address_error(users):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(AddressError, match="service token"):
        await users.email_of(SUB)


@respx.mock
async def test_the_title_comes_from_listings_public_read(http):
    respx.get("http://listings/listings/l-1").mock(
        return_value=httpx.Response(200, json={"id": "l-1", "title": "Levi's 501"})
    )
    assert await ListingTitles(http, "http://listings/").title_of("l-1") == "Levi's 501"


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [httpx.Response(404), httpx.Response(200, json={}), httpx.Response(200, text="<html>")],
)
async def test_any_trouble_reading_the_title_gives_none(http, answer):
    respx.get("http://listings/listings/l-1").mock(return_value=answer)
    assert await ListingTitles(http, "http://listings").title_of("l-1") is None


@respx.mock
async def test_listings_unreachable_gives_none(http):
    respx.get("http://listings/listings/l-1").mock(side_effect=httpx.ConnectError("down"))
    assert await ListingTitles(http, "http://listings").title_of("l-1") is None
