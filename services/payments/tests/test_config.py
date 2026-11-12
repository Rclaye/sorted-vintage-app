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


def test_missing_stripe_key_refuses_to_start(settings):
    bad = dataclasses.replace(settings, stripe_secret_key="", stripe_webhook_secret="")
    with pytest.raises(RuntimeError, match="STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET"):
        create_app(bad, keys=object(), checks={})


def test_webhooks_are_public_paths():
    assert "/webhooks/stripe-connect" in Settings().public_paths


def test_the_account_link_route_is_off_unless_a_client_is_named(monkeypatch):
    assert Settings().account_link_azp == ""
    monkeypatch.setenv("ACCOUNT_LINK_AZP", " seed ")
    assert Settings.from_env().account_link_azp == "seed"
