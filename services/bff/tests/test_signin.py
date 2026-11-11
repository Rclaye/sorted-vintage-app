"""T047: sign-up, sign-in and sign-out through Keycloak's code flow with PKCE, the encrypted
session cookie, refresh, CSRF and the safe-redirect rule. Keycloak's token and logout endpoints
are answered by respx; tokens are signed with the test key the conftest makes."""

import base64
import hashlib
import time
from typing import Annotated
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi import Depends, Request

from app import signin
from app.session import LOGIN_COOKIE, SESSION_COOKIE
from tests.conftest import INTERNAL_REALM, ISSUER, PUBLIC_BASE

TOKEN_URL = f"{INTERNAL_REALM}/protocol/openid-connect/token"
LOGOUT_URL = f"{INTERNAL_REALM}/protocol/openid-connect/logout"
SUB = "6f0c2b8e-0000-4000-8000-000000000001"


@pytest.fixture
def keycloak():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def app_client(client):
    """The BFF with two test-only pages: one for anyone, one that needs sign-in, plus a form."""
    app = client.app

    @app.get("/test/whoami")
    async def whoami(request: Request) -> dict:
        session = await signin.current_session(request)
        return {"sub": session.sub if session else None, "name": session.name if session else None}

    @app.get("/test/private")
    async def private(session: Annotated[signin.Session, Depends(signin.require_session)]) -> dict:
        return {"sub": session.sub, "csrf": session.csrf}

    @app.post("/test/change")
    async def change(
        request: Request, session: Annotated[signin.Session, Depends(signin.require_session)]
    ) -> dict:
        await signin.check_csrf(request, session)
        return {"changed": True}

    client.follow_redirects = False
    return client


def tokens(make_token, nonce: str, azp: str = "bff", expires_in: int = 300) -> dict:
    exp = int(time.time()) + expires_in
    access = make_token(sub=SUB, azp=azp, exp=exp, display_name="Sam V.", email="sam@example.test")
    id_token = make_token(sub=SUB, azp=azp, aud="bff", typ="ID", nonce=nonce, exp=exp)
    return {
        "access_token": access,
        "refresh_token": "refresh-1",
        "id_token": id_token,
        "expires_in": expires_in,
        "refresh_expires_in": 1800,
        "token_type": "Bearer",
    }


def start_login(client, path="/login?next=/sell") -> dict:
    response = client.get(path)
    assert response.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


def sign_in(client, keycloak, make_token, **token_args) -> httpx.Response:
    query = start_login(client)
    keycloak.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=tokens(make_token, query["nonce"], **token_args))
    )
    return client.get(f"/auth/callback?code=the-code&state={query['state']}")


# ---------------------------------------------------------------- starting the flow


def test_login_redirects_to_keycloak_with_pkce(app_client):
    response = app_client.get("/login?next=/sell")

    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{ISSUER}/protocol/openid-connect/auth"
    )
    assert query["client_id"] == ["bff"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{PUBLIC_BASE}/auth/callback"]
    assert "openid" in query["scope"][0]
    assert query["state"][0] and query["nonce"][0] and query["code_challenge"][0]
    cookie = response.headers["set-cookie"]
    assert LOGIN_COOKIE in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=lax" in cookie.lower()


def test_signup_uses_keycloak_registration(app_client):
    location = app_client.get("/signup").headers["location"]
    assert location.startswith(f"{ISSUER}/protocol/openid-connect/registrations?")


# ---------------------------------------------------------------- the callback


def test_callback_signs_in_with_the_verifier_and_sets_a_safe_cookie(
    app_client, keycloak, make_token
):
    query = start_login(app_client)
    exchange = keycloak.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=tokens(make_token, query["nonce"]))
    )

    response = app_client.get(f"/auth/callback?code=the-code&state={query['state']}")

    assert response.status_code == 303
    assert response.headers["location"] == "/sell"
    sent = parse_qs(exchange.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["the-code"]
    verifier = sent["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge.decode() == query["code_challenge"], "PKCE: the verifier matches the challenge"
    assert exchange.calls.last.request.headers["authorization"].startswith("Basic ")
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(SESSION_COOKIE))
    assert "HttpOnly" in session_cookie and "Secure" in session_cookie
    assert "samesite=lax" in session_cookie.lower()
    assert "Path=/" in session_cookie
    assert SUB not in session_cookie, "the cookie is encrypted, not readable"
    assert app_client.get("/test/whoami").json() == {"sub": SUB, "name": "Sam V."}


def test_a_wrong_state_does_not_sign_in(app_client, keycloak, make_token):
    start_login(app_client)
    exchange = keycloak.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))

    response = app_client.get("/auth/callback?code=the-code&state=forged")

    assert response.headers["location"].startswith("/?signin=failed")
    assert exchange.call_count == 0
    assert app_client.get("/test/whoami").json()["sub"] is None


def test_a_token_for_another_client_does_not_sign_in(app_client, keycloak, make_token):
    response = sign_in(app_client, keycloak, make_token, azp="listings")

    assert response.headers["location"].startswith("/?signin=failed")
    assert app_client.get("/test/whoami").json()["sub"] is None


def test_a_replayed_id_token_with_another_nonce_does_not_sign_in(app_client, keycloak, make_token):
    query = start_login(app_client)
    keycloak.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=tokens(make_token, nonce="someone-elses"))
    )

    response = app_client.get(f"/auth/callback?code=c&state={query['state']}")

    assert response.headers["location"].startswith("/?signin=failed")


def test_cancelling_at_keycloak_goes_home(app_client):
    start_login(app_client)
    response = app_client.get("/auth/callback?error=access_denied&state=x")
    assert response.headers["location"].startswith("/?signin=failed")


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil"])
def test_next_never_leaves_the_site(app_client, keycloak, make_token, target):
    query = start_login(app_client, f"/login?next={target}")
    keycloak.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=tokens(make_token, query["nonce"]))
    )
    response = app_client.get(f"/auth/callback?code=c&state={query['state']}")
    assert response.headers["location"] == "/"


# ---------------------------------------------------------------- the session afterwards


def test_signed_out_visitors_are_sent_to_sign_in_and_back(app_client):
    response = app_client.get("/test/private?x=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/test/private%3Fx%3D1"


def test_a_tampered_cookie_counts_as_signed_out(app_client, keycloak, make_token):
    sign_in(app_client, keycloak, make_token)
    value = app_client.cookies.get(SESSION_COOKIE)
    app_client.cookies.clear()
    app_client.cookies.set(SESSION_COOKIE, value[:-4] + "AAAA")
    assert app_client.get("/test/whoami").json()["sub"] is None


def test_an_expiring_access_token_is_refreshed(app_client, keycloak, make_token):
    query = start_login(app_client)
    keycloak.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=tokens(make_token, query["nonce"], expires_in=10)),
            httpx.Response(200, json=tokens(make_token, query["nonce"], expires_in=300)),
        ]
    )
    app_client.get(f"/auth/callback?code=c&state={query['state']}")

    response = app_client.get("/test/whoami")

    assert response.json()["sub"] == SUB
    assert keycloak.calls.call_count == 2
    refresh = parse_qs(keycloak.calls.last.request.content.decode())
    assert refresh["grant_type"] == ["refresh_token"]
    assert any(c.startswith(SESSION_COOKIE) for c in response.headers.get_list("set-cookie"))


def test_a_refused_refresh_signs_out(app_client, keycloak, make_token):
    query = start_login(app_client)
    keycloak.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=tokens(make_token, query["nonce"], expires_in=10)),
            httpx.Response(400, json={"error": "invalid_grant"}),
        ]
    )
    app_client.get(f"/auth/callback?code=c&state={query['state']}")

    response = app_client.get("/test/whoami")

    assert response.json()["sub"] is None
    assert any(
        c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c
        for c in response.headers.get_list("set-cookie")
    ) or any('sv_session=""' in c for c in response.headers.get_list("set-cookie"))


# ---------------------------------------------------------------- CSRF and sign-out


def test_a_form_without_the_csrf_token_is_403(app_client, keycloak, make_token):
    sign_in(app_client, keycloak, make_token)

    response = app_client.post("/test/change", data={"csrf_token": "wrong"})

    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"


def test_a_form_with_the_csrf_token_goes_through(app_client, keycloak, make_token):
    sign_in(app_client, keycloak, make_token)
    csrf = app_client.get("/test/private").json()["csrf"]

    assert app_client.post("/test/change", data={"csrf_token": csrf}).json() == {"changed": True}
    assert app_client.post("/test/change", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_logout_needs_the_csrf_token(app_client, keycloak, make_token):
    sign_in(app_client, keycloak, make_token)

    assert app_client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert app_client.get("/test/whoami").json()["sub"] == SUB


def test_logout_ends_the_keycloak_session_and_clears_the_cookie(app_client, keycloak, make_token):
    sign_in(app_client, keycloak, make_token)
    csrf = app_client.get("/test/private").json()["csrf"]
    end = keycloak.post(LOGOUT_URL).mock(return_value=httpx.Response(204))

    response = app_client.post("/logout", data={"csrf_token": csrf})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert parse_qs(end.calls.last.request.content.decode())["refresh_token"] == ["refresh-1"]
    assert app_client.get("/test/whoami").json()["sub"] is None
