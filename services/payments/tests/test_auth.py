import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import AUDIENCE, AZP


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_is_accepted(client, make_token):
    response = client.get("/ping", headers=bearer(make_token()))
    assert response.status_code == 200
    assert response.json() == {"pong": True}


def test_no_token_is_401(client):
    response = client.get("/ping")
    assert response.status_code == 401
    assert response.json()["error"] == "missing_token"
    assert response.headers["www-authenticate"] == "Bearer"


def test_non_bearer_scheme_is_401(client):
    response = client.get("/ping", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401


def test_garbage_token_is_401(client):
    response = client.get("/ping", headers=bearer("not-a-jwt"))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_expired_token_is_401(client, make_token):
    response = client.get("/ping", headers=bearer(make_token(exp=1)))
    assert response.status_code == 401
    assert response.json()["error"] == "token_expired"


@pytest.mark.skipif(not AUDIENCE, reason="this service checks azp, not aud")
def test_wrong_audience_is_401(client, make_token):
    response = client.get("/ping", headers=bearer(make_token(aud="someone-else")))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


@pytest.mark.skipif(not AUDIENCE, reason="this service checks azp, not aud")
def test_missing_audience_is_401(client, make_token):
    assert client.get("/ping", headers=bearer(make_token(aud=None))).status_code == 401


@pytest.mark.skipif(not AZP, reason="this service checks aud, not azp")
def test_wrong_authorized_party_is_401(client, make_token):
    response = client.get("/ping", headers=bearer(make_token(azp="another-client")))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


@pytest.mark.skipif(not AZP, reason="this service checks aud, not azp")
def test_missing_authorized_party_is_401(client, make_token):
    assert client.get("/ping", headers=bearer(make_token(azp=None))).status_code == 401


@pytest.mark.skipif(not AZP, reason="this service checks aud, not azp")
def test_other_services_audiences_are_fine_when_azp_matches(client, make_token):
    token = make_token(aud=["listings", "orders", "payments"])
    assert client.get("/ping", headers=bearer(token)).status_code == 200


def test_id_token_is_401(client, make_token):
    response = client.get("/ping", headers=bearer(make_token(typ="ID")))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_token_without_typ_is_401(client, make_token):
    assert client.get("/ping", headers=bearer(make_token(typ=None))).status_code == 401


def test_token_without_issuer_is_401(client, make_token):
    assert client.get("/ping", headers=bearer(make_token(iss=None))).status_code == 401


def test_wrong_issuer_is_401(client, make_token):
    assert client.get("/ping", headers=bearer(make_token(iss="http://evil"))).status_code == 401


def test_token_signed_by_another_key_is_401(client, make_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = client.get("/ping", headers=bearer(make_token(signing_key=other)))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_unsigned_token_is_401(client):
    import jwt

    claims = {"aud": AUDIENCE or "x", "azp": AZP, "exp": 9999999999}
    token = jwt.encode(claims, key=None, algorithm="none")
    assert client.get("/ping", headers=bearer(token)).status_code == 401


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_public_paths_need_no_token(client, path):
    assert client.get(path).status_code == 200


def test_unknown_route_without_token_is_401_not_404(client):
    assert client.get("/nope").status_code == 401


def test_unknown_route_with_token_uses_error_body(client, make_token):
    response = client.get("/nope", headers=bearer(make_token()))
    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "message": "Not Found"}
