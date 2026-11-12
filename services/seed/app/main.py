"""Seed job for sv-dev (T051): demo sellers in Keycloak, linked to their Stripe test accounts,
and the demo listings with photos. Safe to run again: users are brought back to the declared
state, links are replaced, and only listings a seller has not published yet are created.

Every request prints one line, `METHOD where -> status`, which the workstation's lab-live pane
shows as it happens. The last line is PASS or FAIL.
"""

import sys
from collections.abc import Callable

import httpx

from app import catalog, photos
from app.config import Seller, Settings, from_env
from app.keycloak import Keycloak
from app.site import LabTransport, Log, SeedError, Site, pinned_context


def say(line: str) -> None:
    print(line, flush=True)


def link(settings: Settings, http: httpx.Client, token: str, sub: str, seller: Seller, log: Log):
    r = http.put(
        f"{settings.payments_url}/connected-accounts/{sub}",
        json={"stripe_account_id": seller.stripe_account_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    status = r.json().get("payout_status", "?") if r.status_code == 200 else r.text[:120]
    log(f"PUT payments/connected-accounts/{seller.key} -> {r.status_code} {status}")
    if r.status_code != 200:
        raise SeedError(f"Payments refused the link for {seller.key}")
    return status


def published_numbers(settings: Settings, http: httpx.Client, sub: str, log: Log) -> set[int]:
    r = http.get(f"{settings.listings_url}/sellers/{sub}")
    log(f"GET listings/sellers/{sub[:8]} -> {r.status_code}")
    if r.status_code == 404:
        return set()
    r.raise_for_status()
    body = r.json()
    titles = [x["title"] for x in body.get("available", []) + body.get("sold", [])]
    return {n for n in map(catalog.number_of, titles) if n is not None}


def run(
    settings: Settings,
    http: httpx.Client,
    browser: Callable[[], httpx.Client],
    log: Log = say,
) -> dict[str, int]:
    kc = Keycloak(settings, http, log)
    token = kc.token()
    subs: dict[str, str] = {}
    for seller in settings.sellers:
        subs[seller.key] = kc.ensure_user(seller, token)
        if seller.stripe_account_id:
            link(settings, http, token, subs[seller.key], seller, log)
        else:
            log(f"{seller.key}: no Stripe account yet; onboard it by hand, then add its id")

    demos = [s for s in settings.sellers if s.is_demo]
    created: dict[str, int] = {}
    for index, seller in enumerate(demos):
        wanted = catalog.numbers_for(index, len(demos), settings.listings_total)
        have = published_numbers(settings, http, subs[seller.key], log)
        missing = [n for n in wanted if n not in have]
        log(f"{seller.key}: {len(wanted) - len(missing)} of {len(wanted)} listings already there")
        created[seller.key] = 0
        if not missing:
            continue
        with browser() as client:
            site = Site(settings.site_url, client, log)
            site.sign_in(seller.key, seller.password)
            for n in missing:
                item = catalog.listing(n)
                listing_id = site.create_listing(
                    {
                        "title": item.title,
                        "brand": item.brand,
                        "category": item.category,
                        "size": item.size,
                        "condition": item.condition,
                        "price": item.price,
                        "description": item.description,
                    }
                )
                site.add_photo(listing_id, f"seed-{n:02d}.png", photos.png(n))
                site.publish(listing_id)
                created[seller.key] += 1
                log(f"{seller.key}: published #{n:02d}")
    return created


def main() -> int:
    settings = from_env()

    def browser() -> httpx.Client:
        transport = LabTransport(
            settings.traefik_host, verify=pinned_context(settings.tls_cert_file)
        )
        return httpx.Client(transport=transport, timeout=30)

    try:
        with httpx.Client(timeout=30) as http:
            created = run(settings, http, browser)
    except (SeedError, httpx.HTTPError) as exc:
        say(f"FAIL: {exc}")
        return 1
    total = sum(created.values())
    say(f"PASS: {len(settings.sellers)} sellers ready, {total} listings created this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
