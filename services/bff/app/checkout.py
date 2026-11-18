"""T073: checkout, the order page and the buyer's orders (contracts/bff-pages.md, FR-009).

Buy posts the item page's form with the session's CSRF token and an Idempotency-Key made when the
page was drawn, so a double click or a resubmitted form reaches the same order in Orders. The pay
page carries Stripe's client secret in the page only: never in a URL, a log line or a cookie. It
loads Stripe.js and /static/checkout.js under its own Content-Security-Policy, which adds the
Stripe origins found in the Ticket 3.1 spike; every other page keeps the strict one.

After paying, Stripe returns the buyer to /orders/{id}, which asks Orders again every 2 seconds
(HTMX) until the order is paid, failed or refunded.
"""

import asyncio
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.pages import MESSAGES, SignedIn, back, render, services
from app.services import ServiceDownError
from app.signin import check_csrf
from app.telemetry import tag_order

log = logging.getLogger(__name__)
router = APIRouter()

FINAL = frozenset({"paid", "failed", "refunded"})
POLL_SECONDS = 2
LIST_LIMIT = 20

# The pay page alone may load Stripe.js and its frames (sprints/sprint-3/spikes.md, T073).
PAY_CSP = (
    "default-src 'self'; img-src 'self' data: https://*.stripe.com; "
    "script-src 'self' https://js.stripe.com https://*.js.stripe.com; "
    "frame-src https://js.stripe.com https://*.js.stripe.com https://hooks.stripe.com; "
    "connect-src 'self' https://api.stripe.com; style-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)

MESSAGES.update(
    {
        "not_available": "Sorry, this item has just been reserved or sold.",
        "own_listing": "This is your own listing, so you can't buy it.",
        "seller_not_ready": "The seller can't take payments yet. Try again later.",
        "checkout_in_progress": "Your checkout is still starting. Wait a moment and try again.",
        "checkout_unavailable": "Checkout isn't available right now. Try again later.",
        "checkout_failed": "Checkout couldn't start. Try again.",
        "payment_not_completed": "Your payment didn't go through. Nothing was charged.",
    }
)

# Order statuses (contracts/orders.openapi.yaml) and failure reasons, in a buyer's words.
STATUS_WORDS = {
    "pending": "Waiting for your payment",
    "paid": "Paid",
    "failed": "Not completed",
    "refunded": "Refunded",
}
DECLINED = "The payment was declined. Nothing was charged, and the item is back on sale."
# failure_reason is Stripe's last_payment_error.code for a failed payment (Payments, T067), or one
# of Orders' own reasons.
REASONS = {
    "card_declined": DECLINED,
    "expired_card": "The card has expired. Nothing was charged, and the item is back on sale.",
    "incorrect_cvc": "The security code was wrong. Nothing was charged, and the item is back on "
    "sale.",
    "payment_failed": DECLINED,
    "payment_canceled": "The payment was cancelled. Nothing was charged, and the item is back on "
    "sale.",
    "reservation_expired": "The 10 minutes to pay ran out, so the item went back on sale.",
    "reservation_released": "The hold on the item ended before the payment arrived, so the "
    "payment was refunded.",
    "seller_not_ready": "The seller can't take payments yet.",
    "payments_unavailable": "Payments couldn't be reached. Nothing was charged.",
    "listings_unavailable": "The listing couldn't be reached. Nothing was charged.",
}
NOT_COMPLETED = "The payment didn't go through. Nothing was charged, and the item is back on sale."


def explain(order: dict) -> str:
    """One sentence for the order page."""
    status = order.get("status")
    if status == "paid":
        return "Thank you. The seller has been told, and a confirmation email is on its way."
    if status == "refunded":
        return REASONS.get(order.get("failure_reason") or "", "This order was refunded in full.")
    if status == "failed":
        return REASONS.get(order.get("failure_reason") or "", NOT_COMPLETED)
    return "This page updates by itself once the payment is confirmed."


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, TypeError):
        return None


async def _title(request: Request, listing_id: str) -> str:
    """The listing's title for the page, best effort: an order page never fails for it."""
    s = services(request)
    try:
        answer = await s.call("GET", f"{s.listings}/listings/{listing_id}")
    except ServiceDownError:
        return "Your item"
    if answer.ok and isinstance(answer.body, dict) and answer.body.get("title"):
        return str(answer.body["title"])
    return "Your item"


def new_idempotency_key() -> str:
    return str(uuid4())


@router.post("/checkout/{listing_id}", response_class=HTMLResponse)
async def checkout(listing_id: str, request: Request, session: SignedIn) -> Response:
    await check_csrf(request, session)
    listing = _uuid(listing_id)
    if listing is None:
        return await render(request, "missing.html", status=404, what="listing")
    item_page = f"/items/{listing}"
    key_publishable = request.app.state.settings.stripe_publishable_key
    if not key_publishable:
        return back(item_page, error="checkout_unavailable")

    form = await request.form()
    # Made when the item page was drawn; a page without one (or with junk) gets a fresh key.
    key = str(_uuid(str(form.get("idempotency_key") or "")) or new_idempotency_key())

    s = services(request)
    answer = await s.call(
        "POST",
        f"{s.orders}/orders",
        token=session.access_token,
        json={"listing_id": str(listing)},
        headers={"Idempotency-Key": key},
    )
    order = answer.body if isinstance(answer.body, dict) else {}
    if not answer.ok:
        error = answer.error if answer.error in MESSAGES else "checkout_failed"
        log.info("checkout refused", extra={"listing_id": str(listing), "reason": answer.error})
        return back(item_page, error=error)
    if order.get("status") != "pending" or not order.get("client_secret"):
        # A replay of an order that is already over: show it as it is.
        return RedirectResponse(f"/orders/{order.get('id')}", status_code=303)

    tag_order(order["id"])
    log.info("checkout started", extra={"order_id": order["id"], "listing_id": str(listing)})
    base = request.app.state.settings.public_base_url
    response = await render(
        request,
        "pay.html",
        order=order,
        title=await _title(request, str(listing)),
        client_secret=order["client_secret"],
        publishable_key=key_publishable,
        return_url=f"{base}/orders/{order['id']}",
        stripe_page=True,
    )
    response.headers["Content-Security-Policy"] = PAY_CSP
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_page(order_id: str, request: Request, session: SignedIn) -> HTMLResponse:
    order_uuid = _uuid(order_id)
    if order_uuid is None:
        return await render(request, "missing.html", status=404, what="order")
    tag_order(order_uuid)
    s = services(request)
    answer = await s.call("GET", f"{s.orders}/orders/{order_uuid}", token=session.access_token)
    if not answer.ok or not isinstance(answer.body, dict):
        return await render(request, "missing.html", status=404, what="order")
    order = answer.body
    context = {
        "order": order,
        "words": STATUS_WORDS.get(order.get("status"), order.get("status")),
        "explanation": explain(order),
        "final": order.get("status") in FINAL,
        "poll_seconds": POLL_SECONDS,
    }
    if request.headers.get("hx-request") == "true":
        # The poll: just the status block, which stops polling once the order is final.
        return await render(request, "_order_status.html", **context)
    # Stripe sends the buyer back here with redirect_status=failed when the payment did not go
    # through (a declined card or a cancelled bank redirect).
    if request.query_params.get("redirect_status") == "failed" and order.get("status") == "pending":
        context["message"] = MESSAGES["payment_not_completed"]
    return await render(
        request, "order.html", title=await _title(request, order["listing_id"]), **context
    )


@router.get("/orders", response_class=HTMLResponse)
async def my_orders(request: Request, session: SignedIn) -> HTMLResponse:
    s = services(request)
    answer = await s.call("GET", f"{s.orders}/orders", token=session.access_token)
    orders = answer.body[:LIST_LIMIT] if answer.ok and isinstance(answer.body, list) else []
    titles = await asyncio.gather(*(_title(request, o["listing_id"]) for o in orders))
    rows = [
        {**o, "title": t, "words": STATUS_WORDS.get(o.get("status"), o.get("status"))}
        for o, t in zip(orders, titles, strict=True)
    ]
    return await render(request, "orders.html", orders=rows)
