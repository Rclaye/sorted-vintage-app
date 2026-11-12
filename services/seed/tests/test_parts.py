import json
import zlib

import httpx
import respx

from app import catalog, photos
from app.config import from_env
from app.site import LabTransport


def test_png_is_a_valid_image_and_repeatable():
    data = photos.png(7, width=40, height=30)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data == photos.png(7, width=40, height=30)
    assert data != photos.png(8, width=40, height=30)
    idat = data.index(b"IDAT")
    size = int.from_bytes(data[idat - 4 : idat], "big")
    raw = zlib.decompress(data[idat + 4 : idat + 4 + size])
    assert len(raw) == 30 * (1 + 40 * 3)


def test_full_size_photo_is_well_under_the_upload_limit():
    assert len(photos.png(1)) < 1_000_000


def test_catalog_deals_every_number_once():
    total, sellers = 50, 2
    dealt = [catalog.numbers_for(i, sellers, total) for i in range(sellers)]
    assert sorted(dealt[0] + dealt[1]) == list(range(1, total + 1))
    assert len(dealt[0]) == len(dealt[1]) == 25


def test_listing_titles_carry_their_number_and_fit_the_rules():
    for n in range(1, 51):
        item = catalog.listing(n)
        assert catalog.number_of(item.title) == n
        assert 3 <= len(item.title) <= 80
        assert len(item.brand) <= 40 and len(item.size) <= 10
        assert item.category in {"tops", "bottoms", "dresses", "outerwear", "shoes", "accessories"}
        assert item.condition in {"new_with_tags", "like_new", "good", "fair"}
        assert 1 <= float(item.price) <= 10_000


def test_number_of_ignores_other_titles():
    assert catalog.number_of("Stussy Shirt") is None
    assert catalog.number_of("#5") is None


def test_settings_read_the_sellers_file(tmp_path):
    f = tmp_path / "sellers.json"
    f.write_text(
        json.dumps(
            [
                {
                    "key": "demo-seller-1",
                    "email": "a@x",
                    "stripe_account_id": "acct_1",
                    "password": "p",
                },
                {
                    "key": "verify-seller",
                    "email": "b@x",
                    "stripe_account_id": "acct_2",
                    "password": "q",
                },
            ]
        )
    )
    s = from_env({"KEYCLOAK_CLIENT_SECRET": "x", "SELLERS_FILE": str(f)})
    assert [x.key for x in s.sellers] == ["demo-seller-1", "verify-seller"]
    assert s.sellers[0].display_name == "Demo Seller 1"
    assert s.sellers[0].is_demo and not s.sellers[1].is_demo
    assert s.listings_total == 50 and s.site_url == "https://dev.sortedvtg.lan:8443"


@respx.mock
def test_lab_names_go_to_traefik_with_their_name_kept():
    route = respx.get("https://10.96.0.9/login").mock(return_value=httpx.Response(200))
    other = respx.get("http://payments/healthz").mock(return_value=httpx.Response(200))
    transport = LabTransport("traefik.traefik.svc", resolve=lambda _: "10.96.0.9")
    with httpx.Client(transport=transport) as client:
        client.get("https://dev.sortedvtg.lan:8443/login")
        client.get("http://payments/healthz")
    sent = route.calls.last.request
    assert sent.headers["host"] == "dev.sortedvtg.lan"
    assert sent.extensions["sni_hostname"] == "dev.sortedvtg.lan"
    assert other.called


def test_verify_seller_comes_from_its_own_secret_until_listed(tmp_path):
    sellers = tmp_path / "sellers.json"
    sellers.write_text(
        json.dumps(
            [
                {
                    "key": "demo-seller-1",
                    "email": "demo-seller-1@sortedvtg.lan",
                    "stripe_account_id": "acct_1",
                    "password": "p",
                }
            ]
        )
    )
    verify = tmp_path / "verify"
    verify.mkdir()
    (verify / "username").write_text("verify-seller@sortedvtg.lan")
    (verify / "password").write_text("vpw")
    env = {"KEYCLOAK_CLIENT_SECRET": "x", "SELLERS_FILE": str(sellers), "VERIFY_DIR": str(verify)}
    s = from_env(env)
    assert [x.key for x in s.sellers] == ["demo-seller-1", "verify-seller"]
    assert s.sellers[1].stripe_account_id == "" and s.sellers[1].password == "vpw"

    listed = json.loads(sellers.read_text()) + [
        {
            "key": "verify-seller",
            "email": "verify-seller@sortedvtg.lan",
            "stripe_account_id": "acct_2",
            "password": "vpw",
        }
    ]
    sellers.write_text(json.dumps(listed))
    s = from_env(env)
    assert [x.stripe_account_id for x in s.sellers] == ["acct_1", "acct_2"]
