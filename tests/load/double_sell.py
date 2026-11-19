"""T058: the double-sell load test (SC-003: an item is never sold twice).

Run on the lab host as bot, through `make loadtest-double-sell ENV=dev` (verify_sprint3.sh does it
from the workstation). Only sv-dev: prod is refused.

1. The verify seller creates ITEMS fresh listings (a small drawn photo each) and publishes them.
   Seeded listings are never touched.
2. The verify buyer fires ATTEMPTS checkouts at them at once (ATTEMPTS / ITEMS per item), each
   with its own Idempotency-Key, through the BFF like a browser.
3. Every checkout that got a PaymentIntent is confirmed with Stripe's test card pm_card_visa,
   using the sandbox secret key (read from a file, never printed).
4. Once the orders settle, Orders' database is asked how many orders are `paid` per listing.

PASS means no listing has more than one paid order. The run also reports how many items sold,
how attempts were refused (`not_available` is the expected answer for the losers) and any errors.

It reuses the seed job's browser code (services/seed/app/site.py: the lab's names sent to
Traefik with TLS pinned to the lab CA), so it needs only the seed service's pinned libraries.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "seed"))

import httpx
from app import photos
from app.site import LAB_SUFFIX, LabTransport, Site, pinned_context

SITE = "https://dev.sortedvtg.lan:8443"
STRIPE = "https://api.stripe.com/v1"
NAMESPACE = "sv-dev"


def quiet(_: str) -> None:
    """Setup requests are not printed one by one; the summary says what happened."""


class AsyncLabTransport(httpx.AsyncHTTPTransport):
    """LabTransport for the async client: *.sortedvtg.lan goes to Traefik, the name kept for
    TLS and the Host header."""

    def __init__(self, traefik_ip: str, **kw):
        super().__init__(**kw)
        self._ip = traefik_ip

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host.endswith(LAB_SUFFIX):
            return await super().handle_async_request(request)
        headers = httpx.Headers(request.headers)
        headers["host"] = host
        inner = httpx.Request(
            request.method,
            request.url.copy_with(host=self._ip, port=443),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await super().handle_async_request(inner)


def kubectl(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["kubectl", "--context", "kind-sorted-vintage", "-n", NAMESPACE, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def secret_value(name: str, key: str) -> str:
    raw = kubectl("get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


def sql(query: str) -> list[list[str]]:
    out = kubectl(
        "exec", "-i", "pg-1", "-c", "postgres", "--",
        "psql", "-U", "postgres", "-d", "orders", "-At", "-F", "\t", "-c", query,
    )  # fmt: skip
    return [line.split("\t") for line in out.splitlines() if line]


def browser(ca: str, traefik_ip: str) -> httpx.Client:
    transport = LabTransport(traefik_ip, resolve=lambda h: h, verify=pinned_context(ca))
    return httpx.Client(transport=transport, timeout=60)


def create_items(seller: Site, count: int, run_id: str) -> list[str]:
    ids = []
    for n in range(count):
        listing_id = seller.create_listing(
            {
                "title": f"Double-sell test {run_id} item {n + 1:03d}",
                "brand": "Test",
                "category": "accessories",
                "size": "OS",
                "condition": "good",
                "price": "5.00",
                "description": "Made by the T058 load test in sv-dev.",
            }
        )
        seller.add_photo(listing_id, f"t058-{n}.png", photos.png(n, 48, 60))
        seller.publish(listing_id)
        ids.append(listing_id)
    return ids


async def attempt(
    client: httpx.AsyncClient, csrf: str, listing_id: str
) -> tuple[str, dict]:
    """One checkout: ('won', {order, secret}) or ('refused', {error}) or ('error', {status})."""
    try:
        r = await client.post(
            f"{SITE}/checkout/{listing_id}",
            data={"csrf_token": csrf, "idempotency_key": str(uuid.uuid4())},
        )
    except httpx.HTTPError as exc:
        return "error", {"status": type(exc).__name__}
    if r.status_code == 200:
        secret = re.search(r'data-client-secret="([^"]+)"', r.text)
        order = re.search(r'data-order-url="/orders/([0-9a-f-]+)"', r.text)
        if secret and order:
            return "won", {"order": order.group(1), "secret": unescape(secret.group(1))}
    if r.status_code == 303:
        error = parse_qs(urlsplit(urljoin(SITE, r.headers["location"])).query).get(
            "error"
        )
        return "refused", {"error": (error or ["redirect"])[0]}
    return "error", {"status": str(r.status_code)}


async def confirm(stripe: httpx.AsyncClient, secret: str) -> int:
    payment_intent = secret.split("_secret_")[0]
    r = await stripe.post(
        f"{STRIPE}/payment_intents/{payment_intent}/confirm",
        data={"payment_method": "pm_card_visa", "return_url": SITE},
    )
    return r.status_code


async def fire(
    args, buyer_cookies, csrf: str, items: list[str], ca: str
) -> list[tuple]:
    transport = AsyncLabTransport(args.traefik_ip, verify=pinned_context(ca))
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    gate = asyncio.Semaphore(args.concurrency)
    targets = [items[i % len(items)] for i in range(args.attempts)]

    async with httpx.AsyncClient(
        transport=transport, cookies=buyer_cookies, timeout=120, limits=limits
    ) as client:

        async def one(listing_id: str):
            async with gate:
                return listing_id, *await attempt(client, csrf, listing_id)

        return await asyncio.gather(*(one(t) for t in targets))


async def confirm_all(secrets: list[str], key: str, per_second: int) -> Counter:
    results: Counter = Counter()
    async with httpx.AsyncClient(auth=(key, ""), timeout=60) as stripe:
        for start in range(0, len(secrets), per_second):
            batch = secrets[start : start + per_second]
            for code in await asyncio.gather(*(confirm(stripe, s) for s in batch)):
                results[code] += 1
            await asyncio.sleep(
                1
            )  # Stripe's test mode allows about 25 requests a second
    return results


def paid_per_listing(items: list[str]) -> dict[str, int]:
    ids = ",".join(f"'{i}'" for i in items)
    rows = sql(
        f"select listing_id, count(*) from orders where status = 'paid' "
        f"and listing_id in ({ids}) group by listing_id"
    )
    return {listing: int(n) for listing, n in rows}


def pending_count(orders: list[str]) -> int:
    if not orders:
        return 0
    ids = ",".join(f"'{o}'" for o in orders)
    return int(
        sql(f"select count(*) from orders where status = 'pending' and id in ({ids})")[
            0
        ][0]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default="dev")
    parser.add_argument("--items", type=int, default=100)
    parser.add_argument("--attempts", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--traefik-ip", default="127.0.0.1")
    parser.add_argument("--settle-seconds", type=int, default=180)
    args = parser.parse_args()
    if args.env != "dev":
        print("FAIL: the double-sell test only ever runs against sv-dev")
        return 2

    ca = os.path.join(
        subprocess.run(
            ["mkcert", "-CAROOT"], capture_output=True, text=True, check=True
        ).stdout.strip(),
        "rootCA.pem",
    )
    run_id = time.strftime("%m%d-%H%M")
    print(
        f"T058 double-sell test {run_id}: {args.attempts} attempts across {args.items} items"
    )

    started = time.monotonic()
    with browser(ca, args.traefik_ip) as c:
        seller = Site(SITE, c, quiet)
        seller.sign_in(
            secret_value("verify-seller", "username"),
            secret_value("verify-seller", "password"),
        )
        items = create_items(seller, args.items, run_id)
    print(
        f"  {len(items)} items published by the verify seller in {time.monotonic() - started:.0f}s"
    )

    with browser(ca, args.traefik_ip) as c:
        buyer = Site(SITE, c, quiet)
        buyer.sign_in(
            secret_value("verify-buyer", "username"),
            secret_value("verify-buyer", "password"),
        )
        cookies, csrf = httpx.Cookies(c.cookies), buyer.csrf

    started = time.monotonic()
    results = asyncio.run(fire(args, cookies, csrf, items, ca))
    took = time.monotonic() - started
    won = [(listing, info) for listing, kind, info in results if kind == "won"]
    refused = Counter(info["error"] for _, kind, info in results if kind == "refused")
    errors = Counter(info["status"] for _, kind, info in results if kind == "error")
    winners_per_item = Counter(listing for listing, _ in won)
    print(f"  {len(results)} attempts in {took:.1f}s: {len(won)} got a payment page, "
          f"refused {dict(refused)}, errors {dict(errors)}")  # fmt: skip

    confirmed = asyncio.run(
        confirm_all(
            [info["secret"] for _, info in won],
            secret_value("stripe", "secret-key"),
            10,
        )
    )
    print(f"  Stripe confirms with pm_card_visa: {dict(confirmed)}")

    orders = [info["order"] for _, info in won]
    deadline = time.monotonic() + args.settle_seconds
    while pending_count(orders) and time.monotonic() < deadline:
        time.sleep(3)
    paid = paid_per_listing(items)
    double = {listing: n for listing, n in paid.items() if n > 1}
    print(f"  after settling: {sum(1 for n in paid.values() if n == 1)} of {len(items)} items "
          f"sold once, {pending_count(orders)} orders still pending")  # fmt: skip
    print(json.dumps({
        "run": run_id, "items": len(items), "attempts": len(results),
        "payment_pages": len(won), "max_payment_pages_per_item": max(winners_per_item.values(), default=0),
        "refused": refused, "errors": errors, "paid_items": len(paid), "double_sold": len(double),
    }))  # fmt: skip
    if double:
        print(f"FAIL: {len(double)} items sold more than once: {sorted(double)[:5]}")
        return 1
    print("PASS: no item was sold twice (SC-003)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
