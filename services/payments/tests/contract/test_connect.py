"""T034: contract tests for Connect onboarding, payout status and the Connect webhook
(contracts/payments.openapi.yaml), plus the T045 reconciler, on a real Postgres.

Stripe is never called: respx answers for https://api.stripe.com. Webhooks are signed here with
the test secret, the way stripe listen signs them (t=<unix time>,v1=<HMAC-SHA256>).
"""

import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import connected_account, make_engine, payment_event
from app.main import create_app
from app.reconcile import reconcile_accounts
from app.stripe_api import StripeConnect, make_client
from tests.conftest import StaticKeys

testcontainers = pytest.importorskip("testcontainers.postgres")

STRIPE = "https://api.stripe.com"
WEBHOOK_SECRET = "whsec_contract_test"
BASE_URL = "https://dev.sortedvtg.lan:8443"


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def database_url():
    try:
        container = testcontainers.PostgresContainer("postgres:17-alpine", driver=None)
        container.start()
    except Exception as exc:  # no Docker on this machine; CI and the lab host have it
        pytest.skip(f"Docker is not available: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def engine(database_url):
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def stripe_api():
    with respx.mock(base_url=STRIPE, assert_all_called=False) as router:
        yield router


def make_api(settings, private_key, engine, **overrides):
    import dataclasses

    return create_app(
        dataclasses.replace(
            settings,
            stripe_webhook_secret=WEBHOOK_SECRET,
            public_base_url=BASE_URL,
            reconcile_interval_seconds=0,
            event_recheck_seconds=0,
            **overrides,
        ),
        keys=StaticKeys(private_key.public_key()),
        checks={},
        engine=engine,
        connect=StripeConnect(make_client("sk_test_contract")),
    )


@pytest.fixture
def api(settings, private_key, engine, stripe_api):
    """The real app on the test database, with migrations run by its lifespan."""
    with TestClient(make_api(settings, private_key, engine), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def dev_api(settings, private_key, engine, stripe_api):
    """The app as sv-dev runs it: the seed client may link accounts (T051)."""
    app = make_api(settings, private_key, engine, account_link_azp="seed")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def seed_client(make_token):
    """The seed job's client-credentials token: azp seed, payments in aud."""
    token = make_token(sub=str(uuid.uuid4()), aud="payments", azp="seed")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seller(make_token):
    """A seller as the BFF's token presents them: subject, payments in aud."""
    sub = uuid.uuid4()
    token = make_token(
        sub=str(sub), aud=["listings", "orders", "payments"], azp="bff", email=f"{sub}@example.test"
    )
    return sub, {"Authorization": f"Bearer {token}"}


@pytest.fixture
def listings_service(make_token):
    """Listings' client-credentials token (T046): azp listings, payments in aud."""
    token = make_token(sub=str(uuid.uuid4()), aud="payments", azp="listings")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- helpers


def account_json(account_id: str, card: str = "restricted", transfers: str = "restricted") -> dict:
    return {
        "id": account_id,
        "object": "v2.core.account",
        "dashboard": "none",
        "configuration": {
            "merchant": {"capabilities": {"card_payments": {"status": card}}},
            "recipient": {
                "capabilities": {"stripe_balance": {"stripe_transfers": {"status": transfers}}}
            },
        },
    }


def stripe_error(message: str) -> httpx.Response:
    return httpx.Response(
        400, json={"error": {"type": "invalid_request_error", "message": message}}
    )


def fake_account_create(account_id: str):
    """Answers like the dev sandbox did on 2026-11-10, including its refusal of an account
    with the recipient configuration and no contact email."""

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "recipient" in body.get("configuration", {}) and not body.get("contact_email"):
            return stripe_error(
                "configuration.recipient: If configuration.recipient is supplied, "
                "the Account must have a contact email."
            )
        return httpx.Response(200, json=account_json(account_id))

    return answer


def fake_account_link(account_id: str):
    """Answers like the dev sandbox did on 2026-11-10: account_onboarding has no
    configurations field in this API version."""

    def answer(request: httpx.Request) -> httpx.Response:
        onboarding = json.loads(request.content).get("use_case", {}).get("account_onboarding", {})
        unknown = set(onboarding) - {"return_url", "refresh_url", "collection_options"}
        if unknown:
            return stripe_error(f"use_case.account_onboarding.{sorted(unknown)[0]}: Unknown field.")
        return httpx.Response(200, json=link_json(account_id))

    return answer


def link_json(account_id: str) -> dict:
    return {
        "object": "v2.core.account_link",
        "account": account_id,
        "url": f"https://connect.stripe.com/setup/{account_id}",
        "expires_at": "2026-11-10T04:00:00.000Z",
        "created": "2026-11-10T03:55:00.000Z",
    }


def new_account_id() -> str:
    return "acct_" + uuid.uuid4().hex[:16]


def thin_event(account_id: str, event_type: str = "v1.account.updated") -> dict:
    return {
        "id": "evt_" + uuid.uuid4().hex,
        "object": "v2.core.event",
        "type": event_type,
        "created": "2026-11-10T03:55:00.000Z",
        "context": account_id,
        "related_object": {
            "id": account_id,
            "type": "account",
            "url": f"/v2/core/accounts/{account_id}",
        },
    }


def signed(payload: bytes, secret: str = WEBHOOK_SECRET, at: int | None = None) -> dict:
    at = int(time.time()) if at is None else at
    mac = hmac.new(secret.encode(), f"{at}.".encode() + payload, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={at},v1={mac}", "Content-Type": "application/json"}


def store_account(engine, sub, account_id, card="restricted", transfers="restricted") -> None:
    with engine.begin() as conn:
        conn.execute(
            connected_account.insert().values(
                account_sub=sub,
                stripe_account_id=account_id,
                card_payments_status=card,
                stripe_transfers_status=transfers,
                updated_at=text("now()"),
            )
        )


def statuses(engine, account_id):
    with engine.connect() as conn:
        return conn.execute(
            select(
                connected_account.c.card_payments_status,
                connected_account.c.stripe_transfers_status,
            ).where(connected_account.c.stripe_account_id == account_id)
        ).first()


def stored_event(engine, event_id):
    with engine.connect() as conn:
        return conn.execute(
            select(payment_event).where(payment_event.c.stripe_event_id == event_id)
        ).first()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == code
    assert body["message"]


# ------------------------------------------- POST /connected-accounts/me/onboarding-link


class TestOnboardingLink:
    def test_creates_the_account_once_and_returns_a_hosted_link(
        self, api, engine, stripe_api, seller
    ):
        sub, auth = seller
        account_id = new_account_id()
        create = stripe_api.post("/v2/core/accounts").mock(
            side_effect=fake_account_create(account_id)
        )
        links = stripe_api.post("/v2/core/account_links").mock(
            side_effect=fake_account_link(account_id)
        )

        first = api.post("/connected-accounts/me/onboarding-link", headers=auth)

        assert first.status_code == 200, first.text
        assert first.json() == {
            "url": f"https://connect.stripe.com/setup/{account_id}",
            "expires_at": "2026-11-10T04:00:00.000Z",
        }
        sent = json.loads(create.calls.last.request.content)
        assert sent["contact_email"] == f"{sub}@example.test"
        assert sent["dashboard"] == "none"
        assert sent["identity"] == {"country": "us"}
        assert sent["defaults"]["responsibilities"] == {
            "fees_collector": "application",
            "losses_collector": "stripe",
        }
        assert sent["configuration"]["merchant"]["capabilities"]["card_payments"]["requested"]
        assert sent["configuration"]["recipient"]["capabilities"]["stripe_balance"][
            "stripe_transfers"
        ]["requested"]
        assert create.calls.last.request.headers["Idempotency-Key"] == f"connected-account-{sub}"
        link = json.loads(links.calls.last.request.content)
        assert link["account"] == account_id
        assert link["use_case"]["type"] == "account_onboarding"
        assert link["use_case"]["account_onboarding"]["return_url"] == f"{BASE_URL}/sell/onboarding"
        assert (
            link["use_case"]["account_onboarding"]["refresh_url"]
            == f"{BASE_URL}/sell/onboarding?refresh=1"
        )
        assert statuses(engine, account_id) == ("restricted", "restricted")

        second = api.post("/connected-accounts/me/onboarding-link", headers=auth)

        assert second.status_code == 200
        assert create.call_count == 1, "the account is created once per seller"
        assert links.call_count == 2, "every call returns a fresh link"

    def test_needs_a_token(self, api):
        assert_error(api.post("/connected-accounts/me/onboarding-link"), 401, "missing_token")

    def test_stripe_unreachable_is_503_and_stores_nothing(self, api, engine, stripe_api, seller):
        sub, auth = seller
        stripe_api.post("/v2/core/accounts").mock(side_effect=httpx.ConnectError("down"))

        response = api.post("/connected-accounts/me/onboarding-link", headers=auth)

        assert_error(response, 503, "provider_unavailable")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(connected_account).where(connected_account.c.account_sub == sub)
                ).first()
                is None
            )

    def test_stripe_server_error_is_503(self, api, stripe_api, seller):
        _, auth = seller
        stripe_api.post("/v2/core/accounts").mock(
            return_value=httpx.Response(
                500, json={"error": {"type": "api_error", "message": "boom"}}
            )
        )
        assert_error(
            api.post("/connected-accounts/me/onboarding-link", headers=auth),
            503,
            "provider_unavailable",
        )

    # ------------------------------------------- GET /connected-accounts/{sub}/payout-status

    def test_a_token_without_an_email_is_422_and_creates_nothing(
        self, api, engine, stripe_api, make_token
    ):
        sub = uuid.uuid4()
        token = make_token(sub=str(sub), aud=["payments"], azp="bff")
        create = stripe_api.post("/v2/core/accounts").mock(
            side_effect=fake_account_create(new_account_id())
        )

        response = api.post(
            "/connected-accounts/me/onboarding-link", headers={"Authorization": f"Bearer {token}"}
        )

        assert_error(response, 422, "email_required")
        assert create.call_count == 0
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(connected_account).where(connected_account.c.account_sub == sub)
                ).first()
                is None
            )


class TestPayoutStatus:
    def test_none_without_an_account(self, api, seller):
        sub, auth = seller
        response = api.get(f"/connected-accounts/{sub}/payout-status", headers=auth)
        assert response.status_code == 200
        assert response.json() == {"payout_status": "none"}

    @pytest.mark.parametrize(
        ("card", "transfers", "expected"),
        [
            ("restricted", "restricted", "pending"),
            ("active", "pending", "pending"),
            ("pending", "active", "pending"),
            ("active", "active", "ready"),
        ],
    )
    def test_derived_from_both_capabilities(self, api, engine, seller, card, transfers, expected):
        sub, auth = seller
        store_account(engine, sub, new_account_id(), card, transfers)
        response = api.get(f"/connected-accounts/{sub}/payout-status", headers=auth)
        assert response.json() == {"payout_status": expected}

    def test_listings_reads_any_seller(self, api, engine, listings_service):
        sub = uuid.uuid4()
        store_account(engine, sub, new_account_id(), "active", "active")
        response = api.get(f"/connected-accounts/{sub}/payout-status", headers=listings_service)
        assert response.json() == {"payout_status": "ready"}

    def test_another_seller_is_403(self, api, seller):
        _, auth = seller
        assert_error(
            api.get(f"/connected-accounts/{uuid.uuid4()}/payout-status", headers=auth),
            403,
            "forbidden",
        )

    def test_needs_a_token(self, api):
        assert_error(
            api.get(f"/connected-accounts/{uuid.uuid4()}/payout-status"), 401, "missing_token"
        )

    def test_a_token_for_another_service_is_401(self, api, make_token):
        token = make_token(sub=str(uuid.uuid4()), aud="orders")
        response = api.get(
            f"/connected-accounts/{uuid.uuid4()}/payout-status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert_error(response, 401, "invalid_token")

    def test_not_a_uuid_is_422(self, api, seller):
        _, auth = seller
        assert api.get("/connected-accounts/nope/payout-status", headers=auth).status_code == 422


# ---------------------------------------------------------------- PUT /connected-accounts/{sub}


def owner_of(engine, account_id):
    with engine.connect() as conn:
        return conn.execute(
            select(connected_account.c.account_sub).where(
                connected_account.c.stripe_account_id == account_id
            )
        ).scalar_one_or_none()


def fake_account_read(account_id: str, card: str = "active", transfers: str = "active"):
    return httpx.Response(200, json=account_json(account_id, card, transfers))


class TestLinkAccount:
    def test_links_a_seller_to_an_account_stripe_knows(
        self, dev_api, engine, stripe_api, seed_client
    ):
        sub, account_id = uuid.uuid4(), new_account_id()
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=fake_account_read(account_id)
        )

        response = dev_api.put(
            f"/connected-accounts/{sub}",
            json={"stripe_account_id": account_id},
            headers=seed_client,
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"payout_status": "ready"}
        assert owner_of(engine, account_id) == sub
        assert statuses(engine, account_id) == ("active", "active")
        assert "include" in str(fetch.calls.last.request.url)

    def test_stores_what_stripe_reports_not_ready_included(
        self, dev_api, engine, stripe_api, seed_client
    ):
        sub, account_id = uuid.uuid4(), new_account_id()
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=fake_account_read(account_id, "active", "restricted")
        )

        response = dev_api.put(
            f"/connected-accounts/{sub}",
            json={"stripe_account_id": account_id},
            headers=seed_client,
        )

        assert response.json() == {"payout_status": "pending"}
        assert statuses(engine, account_id) == ("active", "restricted")

    def test_the_same_call_again_changes_nothing(self, dev_api, engine, stripe_api, seed_client):
        sub, account_id = uuid.uuid4(), new_account_id()
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=fake_account_read(account_id)
        )
        body = {"stripe_account_id": account_id}

        first = dev_api.put(f"/connected-accounts/{sub}", json=body, headers=seed_client)
        second = dev_api.put(f"/connected-accounts/{sub}", json=body, headers=seed_client)

        assert first.json() == second.json() == {"payout_status": "ready"}
        with engine.connect() as conn:
            rows = conn.execute(
                select(connected_account).where(connected_account.c.account_sub == sub)
            ).all()
        assert len(rows) == 1

    def test_an_account_moves_from_its_old_seller(self, dev_api, engine, stripe_api, seed_client):
        """After a rebuild the same person has a new Keycloak id; the account follows them."""
        old, new, account_id = uuid.uuid4(), uuid.uuid4(), new_account_id()
        store_account(engine, old, account_id, "active", "active")
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=fake_account_read(account_id)
        )

        response = dev_api.put(
            f"/connected-accounts/{new}",
            json={"stripe_account_id": account_id},
            headers=seed_client,
        )

        assert response.status_code == 200, response.text
        assert owner_of(engine, account_id) == new
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(connected_account).where(connected_account.c.account_sub == old)
                ).first()
                is None
            )

    def test_a_seller_with_another_account_gets_the_linked_one(
        self, dev_api, engine, stripe_api, seed_client
    ):
        sub, before, after = uuid.uuid4(), new_account_id(), new_account_id()
        store_account(engine, sub, before)
        stripe_api.get(f"/v2/core/accounts/{after}").mock(return_value=fake_account_read(after))

        response = dev_api.put(
            f"/connected-accounts/{sub}", json={"stripe_account_id": after}, headers=seed_client
        )

        assert response.json() == {"payout_status": "ready"}
        assert owner_of(engine, after) == sub
        assert owner_of(engine, before) is None

    def test_off_by_default_is_404_even_for_the_seed_client(
        self, api, engine, stripe_api, seed_client
    ):
        account_id = new_account_id()
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=fake_account_read(account_id)
        )

        response = api.put(
            f"/connected-accounts/{uuid.uuid4()}",
            json={"stripe_account_id": account_id},
            headers=seed_client,
        )

        assert_error(response, 404, "not_found")
        assert fetch.call_count == 0
        assert owner_of(engine, account_id) is None

    def test_a_seller_or_another_service_is_403(
        self, dev_api, engine, stripe_api, seller, listings_service
    ):
        sub, auth = seller
        account_id = new_account_id()
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=fake_account_read(account_id)
        )
        body = {"stripe_account_id": account_id}

        for headers in (auth, listings_service):
            assert_error(
                dev_api.put(f"/connected-accounts/{sub}", json=body, headers=headers),
                403,
                "forbidden",
            )
        assert fetch.call_count == 0
        assert owner_of(engine, account_id) is None

    def test_needs_a_token(self, dev_api):
        response = dev_api.put(
            f"/connected-accounts/{uuid.uuid4()}", json={"stripe_account_id": new_account_id()}
        )
        assert_error(response, 401, "missing_token")

    def test_an_account_stripe_does_not_know_is_422_and_stores_nothing(
        self, dev_api, engine, stripe_api, seed_client
    ):
        sub, account_id = uuid.uuid4(), new_account_id()
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=httpx.Response(
                404, json={"error": {"type": "invalid_request_error", "message": "No such account"}}
            )
        )

        response = dev_api.put(
            f"/connected-accounts/{sub}",
            json={"stripe_account_id": account_id},
            headers=seed_client,
        )

        assert_error(response, 422, "unknown_stripe_account")
        assert owner_of(engine, account_id) is None

    def test_stripe_unreachable_is_503_and_keeps_the_old_link(
        self, dev_api, engine, stripe_api, seed_client
    ):
        old, account_id = uuid.uuid4(), new_account_id()
        store_account(engine, old, account_id)
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            side_effect=httpx.ConnectError("down")
        )

        response = dev_api.put(
            f"/connected-accounts/{uuid.uuid4()}",
            json={"stripe_account_id": account_id},
            headers=seed_client,
        )

        assert_error(response, 503, "provider_unavailable")
        assert owner_of(engine, account_id) == old

    @pytest.mark.parametrize("bad", ["", "cus_123456789", "acct_", "acct_../v2/core/events"])
    def test_something_that_is_not_an_account_id_is_422(
        self, dev_api, stripe_api, seed_client, bad
    ):
        response = dev_api.put(
            f"/connected-accounts/{uuid.uuid4()}",
            json={"stripe_account_id": bad},
            headers=seed_client,
        )
        assert_error(response, 422, "invalid_account_id")
        assert not stripe_api.calls


# ---------------------------------------------------------------- POST /webhooks/stripe-connect


class TestConnectWebhook:
    def test_a_signed_account_event_is_stored_and_applied(self, api, engine, stripe_api):
        account_id = new_account_id()
        store_account(engine, uuid.uuid4(), account_id)
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=httpx.Response(200, json=account_json(account_id, "active", "active"))
        )
        event = thin_event(account_id)
        payload = json.dumps(event).encode()

        response = api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))

        assert response.status_code == 200, response.text
        assert statuses(engine, account_id) == ("active", "active")
        assert "include" in str(fetch.calls.last.request.url)
        row = stored_event(engine, event["id"])
        assert row.source == "connect_webhook"
        assert row.type == "v1.account.updated"
        assert row.applied_at is not None

    def test_an_account_not_ready_yet_is_read_again_after_the_event(self, api, engine, stripe_api):
        """Stripe's account read can lag its capability events (dev sandbox, 2026-11-10)."""
        account_id = new_account_id()
        store_account(engine, uuid.uuid4(), account_id)
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            side_effect=[
                httpx.Response(200, json=account_json(account_id, "restricted", "restricted")),
                httpx.Response(200, json=account_json(account_id, "active", "active")),
            ]
        )
        payload = json.dumps(thin_event(account_id, "v1.capability.updated")).encode()

        response = api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))

        assert response.status_code == 200
        assert fetch.call_count == 2, "a second read follows when the first is not ready"
        assert statuses(engine, account_id) == ("active", "active")

    def test_a_ready_account_is_not_read_again(self, api, engine, stripe_api):
        account_id = new_account_id()
        store_account(engine, uuid.uuid4(), account_id)
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=httpx.Response(200, json=account_json(account_id, "active", "active"))
        )
        payload = json.dumps(thin_event(account_id)).encode()

        api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))

        assert fetch.call_count == 1

    def test_a_capability_event_is_applied_too(self, api, engine, stripe_api):
        account_id = new_account_id()
        store_account(engine, uuid.uuid4(), account_id)
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=httpx.Response(200, json=account_json(account_id, "active", "pending"))
        )
        payload = json.dumps(thin_event(account_id, "v1.capability.updated")).encode()

        assert (
            api.post(
                "/webhooks/stripe-connect", content=payload, headers=signed(payload)
            ).status_code
            == 200
        )
        assert statuses(engine, account_id) == ("active", "pending")

    def test_a_replayed_event_is_a_no_op(self, api, engine, stripe_api):
        account_id = new_account_id()
        store_account(engine, uuid.uuid4(), account_id)
        fetch = stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            return_value=httpx.Response(200, json=account_json(account_id, "active", "active"))
        )
        payload = json.dumps(thin_event(account_id)).encode()

        first = api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))
        second = api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))

        assert first.status_code == second.status_code == 200
        assert fetch.call_count == 1
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("select count(*) from payment_event where payload->>'context' = :a"),
                    {"a": account_id},
                ).scalar_one()
                == 1
            )

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({"Content-Type": "application/json"}, id="no signature"),
            pytest.param(None, id="wrong secret"),
            pytest.param("stale", id="older than five minutes"),
            pytest.param(
                {"Stripe-Signature": "garbage", "Content-Type": "application/json"}, id="malformed"
            ),
        ],
    )
    def test_a_bad_signature_is_400_and_stores_nothing(self, api, engine, stripe_api, headers):
        account_id = new_account_id()
        event = thin_event(account_id)
        payload = json.dumps(event).encode()
        if headers is None:
            headers = signed(payload, secret="whsec_someone_else")
        elif headers == "stale":
            headers = signed(payload, at=int(time.time()) - 600)

        response = api.post("/webhooks/stripe-connect", content=payload, headers=headers)

        assert_error(response, 400, "invalid_signature")
        assert stored_event(engine, event["id"]) is None
        assert not stripe_api.calls

    def test_stored_but_stripe_down_still_answers_200_and_keeps_the_event(
        self, api, engine, stripe_api
    ):
        account_id = new_account_id()
        store_account(engine, uuid.uuid4(), account_id)
        stripe_api.get(f"/v2/core/accounts/{account_id}").mock(
            side_effect=httpx.ConnectError("down")
        )
        event = thin_event(account_id)
        payload = json.dumps(event).encode()

        response = api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))

        assert response.status_code == 200
        row = stored_event(engine, event["id"])
        assert row is not None and row.applied_at is None
        assert statuses(engine, account_id) == ("restricted", "restricted")

    def test_needs_no_token(self, api, engine, stripe_api):
        payload = json.dumps(thin_event(new_account_id(), "v1.other.event")).encode()
        response = api.post("/webhooks/stripe-connect", content=payload, headers=signed(payload))
        assert response.status_code == 200


# ---------------------------------------------------------------- T045, not a route


class TestReconciliation:
    def test_refreshes_only_accounts_that_are_not_ready(self, api, engine, stripe_api):
        waiting, ready = new_account_id(), new_account_id()
        store_account(engine, uuid.uuid4(), waiting)
        store_account(engine, uuid.uuid4(), ready, "active", "active")
        stripe_api.get(f"/v2/core/accounts/{waiting}").mock(
            return_value=httpx.Response(200, json=account_json(waiting, "active", "active"))
        )
        untouched = stripe_api.get(f"/v2/core/accounts/{ready}").mock(
            return_value=httpx.Response(200, json=account_json(ready, "restricted", "restricted"))
        )
        # Other tests' leftover accounts answer too, so the pass can finish.
        stripe_api.get(url__regex=r"/v2/core/accounts/acct_\w+").mock(
            side_effect=lambda request: httpx.Response(
                200, json=account_json(request.url.path.rsplit("/", 1)[-1])
            )
        )

        reconcile_accounts(engine, api.app.state.connect)

        assert statuses(engine, waiting) == ("active", "active")
        assert statuses(engine, ready) == ("active", "active")
        assert untouched.call_count == 0
