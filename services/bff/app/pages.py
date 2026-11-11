"""The User Story 1 pages (T048, contracts/bff-pages.md): browse and search, a listing, a seller,
selling (draft, photos, publish), Stripe onboarding, and photo bytes.

Pages render Jinja templates; search answers HTMX requests with just the results. Every form that
changes something carries the session's CSRF token. Errors from Listings and Payments come back
to the page as a short message, never as a stack trace.
"""

import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from app.services import Services
from app.session import Session
from app.signin import check_csrf, current_session, require_session, safe_next

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SignedIn = Annotated[Session, Depends(require_session)]

CATEGORIES = ["tops", "bottoms", "dresses", "outerwear", "shoes", "accessories"]
CONDITIONS = {
    "new_with_tags": "New with tags",
    "like_new": "Like new",
    "good": "Good",
    "fair": "Fair",
}

# Machine codes from the services, in words a seller understands.
MESSAGES = {
    "validation_failed": "Some details are missing or not valid. Check the form and try again.",
    "photo_type_rejected": "Photos must be JPEG, PNG or WebP images.",
    "photo_too_large": "Photos can be at most 10 MB.",
    "photo_limit": "A listing can have at most 8 photos.",
    "photos_required": "Add at least one photo before publishing.",
    "listing_locked": "This listing is reserved or sold and can no longer be changed.",
    "not_a_draft": "This listing is already published.",
    "payout_status_unavailable": "Payouts can't be checked right now. Try again in a moment.",
    "provider_unavailable": "Stripe can't be reached right now. Try again in a moment.",
    "email_required": "Your account has no email address, which Stripe needs.",
    "upload_failed": "The photo could not be read. Try again.",
    "no_file": "Choose a photo first.",
}


def services(request: Request) -> Services:
    return request.app.state.services


async def render(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
    session = await current_session(request)
    context.setdefault("message", MESSAGES.get(request.query_params.get("error", ""), ""))
    return templates.TemplateResponse(
        request,
        name,
        {"session": session, "conditions": CONDITIONS, "categories": CATEGORIES, **context},
        status_code=status,
    )


def back(path: str, **params: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def dollars_to_cents(value: str | None) -> int | None:
    try:
        return round(float(value) * 100) if value not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------- browse and search (FR-007)


async def _search(request: Request) -> HTMLResponse:
    q = request.query_params
    params = {
        "q": q.get("q", "").strip(),
        "brand": q.get("brand", "").strip(),
        "size": q.get("size", "").strip(),
        "category": q.get("category", "") if q.get("category") in CATEGORIES else "",
        "min_price_cents": dollars_to_cents(q.get("min")),
        "max_price_cents": dollars_to_cents(q.get("max")),
        "cursor": q.get("cursor", ""),
        "limit": 24,
    }
    params = {k: v for k, v in params.items() if v not in ("", None)}
    answer = await services(request).call(
        "GET", f"{services(request).listings}/listings", params=params
    )
    page = answer.body if answer.ok else {"items": [], "next_cursor": None}
    filters = {k: q.get(k, "") for k in ("q", "brand", "size", "category", "min", "max")}
    next_query = urlencode({**filters, "cursor": page.get("next_cursor") or ""})
    name = "_results.html" if request.headers.get("hx-request") else "index.html"
    return await render(
        request,
        name,
        items=page.get("items", []),
        next_cursor=page.get("next_cursor"),
        next_query=next_query,
        filters=filters,
        bad_filter=not answer.ok,
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return await _search(request)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request) -> HTMLResponse:
    return await _search(request)


# ---------------------------------------------------------------- a listing, a seller


@router.get("/items/{listing_id}", response_class=HTMLResponse)
async def item(listing_id: str, request: Request) -> HTMLResponse:
    session = await current_session(request)
    answer = await services(request).call(
        "GET",
        f"{services(request).listings}/listings/{quote(listing_id, safe='')}",
        token=session.access_token if session else None,
    )
    if not answer.ok:
        return await render(request, "missing.html", status=404, what="listing")
    listing = answer.body
    own = bool(session and session.sub == listing.get("seller_sub"))
    return await render(
        request,
        "item.html",
        listing=listing,
        own=own,
        published=request.query_params.get("published") == "1",
    )


@router.get("/sellers/{account_sub}", response_class=HTMLResponse)
async def seller(account_sub: str, request: Request) -> HTMLResponse:
    answer = await services(request).call(
        "GET", f"{services(request).listings}/sellers/{quote(account_sub, safe='')}"
    )
    if not answer.ok:
        return await render(request, "missing.html", status=404, what="seller")
    return await render(request, "seller.html", profile=answer.body)


# ---------------------------------------------------------------- selling (FR-004, FR-005, FR-014)


@router.get("/sell", response_class=HTMLResponse)
async def sell(request: Request, session: SignedIn) -> HTMLResponse:
    svc = services(request)
    mine = await svc.call(
        "GET", f"{svc.listings}/sellers/{session.sub}", token=session.access_token
    )
    profile = mine.body if mine.ok else {"drafts": [], "available": [], "sold": []}
    payout = await svc.call(
        "GET",
        f"{svc.payments}/connected-accounts/{session.sub}/payout-status",
        token=session.access_token,
    )
    payout_status = payout.body.get("payout_status", "none") if payout.ok else "unknown"
    return await render(
        request,
        "sell.html",
        profile=profile,
        payout_status=payout_status,
        payouts_ready=request.query_params.get("payouts") == "ready",
    )


@router.post("/sell/listings")
async def create_listing(request: Request, session: SignedIn) -> Response:
    await check_csrf(request, session)
    form = await request.form()
    body = {
        "title": str(form.get("title", "")).strip(),
        "brand": str(form.get("brand", "")).strip(),
        "category": str(form.get("category", "")),
        "size": str(form.get("size", "")).strip(),
        "condition": str(form.get("condition", "")),
        "price_cents": dollars_to_cents(str(form.get("price", ""))),
        "description": str(form.get("description", "")).strip(),
    }
    svc = services(request)
    answer = await svc.call(
        "POST", f"{svc.listings}/listings", token=session.access_token, json=body
    )
    if answer.status == 201:
        return back(f"/items/{answer.body['id']}")
    return back("/sell", error=answer.error or "validation_failed")


@router.post("/sell/listings/{listing_id}/photos")
async def upload_photo(listing_id: str, request: Request, session: SignedIn) -> Response:
    await check_csrf(request, session)
    form = await request.form()
    target = f"/items/{quote(listing_id, safe='')}"
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return back(target, error="no_file")
    data = await upload.read()
    svc = services(request)
    answer = await svc.call(
        "POST",
        f"{svc.listings}/listings/{quote(listing_id, safe='')}/photos",
        token=session.access_token,
        files={"file": (upload.filename, data, upload.content_type or "application/octet-stream")},
    )
    return (
        back(target)
        if answer.status == 201
        else back(target, error=answer.error or "upload_failed")
    )


@router.post("/sell/listings/{listing_id}/publish")
async def publish(listing_id: str, request: Request, session: SignedIn) -> Response:
    await check_csrf(request, session)
    target = f"/items/{quote(listing_id, safe='')}"
    svc = services(request)
    answer = await svc.call(
        "POST",
        f"{svc.listings}/listings/{quote(listing_id, safe='')}/publish",
        token=session.access_token,
    )
    if answer.ok:
        return back(target, published="1")
    if answer.error == "payout_onboarding_required":
        return back("/sell/onboarding", next=target)
    return back(target, error=answer.error or "validation_failed")


@router.get("/sell/onboarding")
async def onboarding(request: Request, session: SignedIn) -> Response:
    """Sends the seller to Stripe's hosted onboarding, and is where Stripe sends them back
    (return_url and refresh_url): when payouts are ready, on to where they were going."""
    svc = services(request)
    status = await svc.call(
        "GET",
        f"{svc.payments}/connected-accounts/{session.sub}/payout-status",
        token=session.access_token,
    )
    if status.ok and status.body.get("payout_status") == "ready":
        wanted = request.query_params.get("next")
        return back(safe_next(wanted) if wanted else "/sell", payouts="ready")
    link = await svc.call(
        "POST", f"{svc.payments}/connected-accounts/me/onboarding-link", token=session.access_token
    )
    if link.ok and link.body.get("url", "").startswith("https://"):
        return RedirectResponse(link.body["url"], status_code=303)
    return back("/sell", error=link.error or "provider_unavailable")


# ---------------------------------------------------------------- photos (decided 2026-11-11)


@router.get("/photos/{path:path}")
async def photo(path: str, request: Request) -> Response:
    """Forwards to Listings, which reads Garage; a draft's photos need the seller's token."""
    if ".." in path or path.startswith("/"):
        return Response(status_code=404)
    session = await current_session(request)
    upstream = await services(request).photo(path, session.access_token if session else None)
    if upstream.status_code != 200:
        return Response(status_code=404)
    headers = {"X-Content-Type-Options": "nosniff"}
    if upstream.headers.get("cache-control"):
        headers["Cache-Control"] = upstream.headers["cache-control"]
    return Response(
        upstream.content,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )
