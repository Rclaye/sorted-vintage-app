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
        create_app(settings, checks={})


def test_default_settings_are_not_valid_without_an_issuer():
    with pytest.raises(RuntimeError):
        Settings().validate()


def test_a_missing_database_queue_or_client_secret_refuses_to_start(settings):
    bad = dataclasses.replace(settings, database_url="", amqp_url="", keycloak_client_secret="")
    with pytest.raises(RuntimeError, match="DATABASE_URL, AMQP_URL, KEYCLOAK_CLIENT_SECRET"):
        bad.validate()


def test_the_admin_url_comes_from_the_in_cluster_token_url(settings):
    s = dataclasses.replace(
        settings,
        keycloak_token_url="http://keycloak.keycloak/realms/sv-dev/protocol/openid-connect/token",
    )
    assert s.admin_url == "http://keycloak.keycloak/admin/realms/sv-dev"
    assert dataclasses.replace(s, keycloak_admin_url="http://kc/admin/realms/x/").admin_url == (
        "http://kc/admin/realms/x"
    )


def test_without_a_token_url_the_issuer_is_used(settings):
    assert settings.token_url == settings.jwt_issuer + "/protocol/openid-connect/token"
    assert settings.admin_url == "http://keycloak.test/admin/realms/sv-test"


def test_settings_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mailpit.sv-dev")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("PREFETCH", "3")
    monkeypatch.setenv("LISTINGS_URL", "http://listings/")
    s = Settings.from_env()
    assert (s.smtp_host, s.smtp_port, s.prefetch, s.listings_url) == (
        "mailpit.sv-dev",
        2525,
        3,
        "http://listings",
    )
