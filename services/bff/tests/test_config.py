import dataclasses

import pytest

from app.config import Settings
from app.main import create_app


def test_empty_issuer_refuses_to_start(settings):
    with pytest.raises(RuntimeError, match="JWT_ISSUER"):
        create_app(dataclasses.replace(settings, jwt_issuer=""), keys=object(), checks={})


def test_no_audience_and_no_azp_refuses_to_start(settings):
    bad = dataclasses.replace(settings, jwt_audience="", jwt_azp="")
    with pytest.raises(RuntimeError, match="JWT_AUDIENCE or JWT_AZP"):
        create_app(bad, keys=object(), checks={})


def test_missing_jwks_url_refuses_to_start(settings):
    with pytest.raises(RuntimeError, match="JWKS_URL"):
        create_app(dataclasses.replace(settings, jwks_url=""), checks={})


def test_missing_sign_in_settings_refuse_to_start(settings):
    bad = dataclasses.replace(settings, client_secret="", session_cookie_key="")
    with pytest.raises(RuntimeError, match="KEYCLOAK_CLIENT_SECRET, .*SESSION_COOKIE_KEY"):
        create_app(bad, keys=object(), checks={})


def test_a_short_cookie_key_refuses_to_start(settings):
    with pytest.raises(RuntimeError, match="too short"):
        create_app(
            dataclasses.replace(settings, session_cookie_key="short"), keys=object(), checks={}
        )


def test_realm_urls_come_from_the_issuer_and_jwks_url(settings):
    assert settings.realm_public_url == "http://keycloak.test/realms/sv-test"
    assert settings.realm_internal_url == "http://keycloak.internal/realms/sv-test"


def test_default_settings_are_not_valid_without_an_issuer():
    with pytest.raises(RuntimeError):
        Settings().validate()
