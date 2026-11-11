"""Sign-up, sign-in and sign-out (T047, FR-001, contracts/bff-pages.md).

GET /login and GET /signup start Keycloak's code flow with PKCE; GET /auth/callback finishes it
and sets the session cookie; POST /logout (with the CSRF token) ends it. Pages use
current_session (anyone, maybe signed in), require_session (redirects to sign-in) and
check_csrf (every form that changes something).
"""

import hmac
import logging
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.oidc import Keycloak, SignInError, pkce_pair
from app.session import LOGIN_COOKIE, SESSION_COOKIE, Session, SessionCodec, new_csrf

log = logging.getLogger(__name__)
router = APIRouter()


class SignInRequiredError(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


class CsrfError(Exception):
    pass


def safe_next(value: str | None) -> str:
    """Only paths on this site, so the sign-in can't be used to send people elsewhere."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _redirect_uri(request: Request) -> str:
    return f"{request.app.state.settings.public_base_url}/auth/callback"


async def _start(request: Request, signup: bool) -> Response:
    keycloak: Keycloak = request.app.state.keycloak
    codec: SessionCodec = request.app.state.codec
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    response = RedirectResponse(
        keycloak.authorize_url(_redirect_uri(request), state, challenge, nonce, signup=signup),
        status_code=303,
    )
    response.set_cookie(
        LOGIN_COOKIE,
        codec.encode_login(state, verifier, nonce, safe_next(request.query_params.get("next"))),
        max_age=600,
        path="/auth",
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/login")
async def login(request: Request) -> Response:
    return await _start(request, signup=False)


@router.get("/signup")
async def signup(request: Request) -> Response:
    """Keycloak's registration page; finishing it signs the new user in (same code flow)."""
    return await _start(request, signup=True)


@router.get("/auth/callback")
async def callback(request: Request) -> Response:
    codec: SessionCodec = request.app.state.codec
    keycloak: Keycloak = request.app.state.keycloak
    pending = codec.decode_login(request.cookies.get(LOGIN_COOKIE))
    params = request.query_params
    if params.get("error"):
        log.info("sign-in cancelled at Keycloak", extra={"reason": params.get("error")})
        return _back_home(request)
    if not pending or not hmac.compare_digest(pending["state"], params.get("state", "")):
        log.warning("sign-in callback with a missing or wrong state")
        return _back_home(request)
    try:
        tokens = await keycloak.exchange_code(
            params.get("code", ""), pending["verifier"], _redirect_uri(request)
        )
        claims = keycloak.check(tokens.access_token)
        keycloak.check(tokens.id_token, nonce=pending["nonce"])
    except SignInError as exc:
        log.warning("sign-in failed", extra={"reason": str(exc)})
        return _back_home(request)

    session = Session(
        sub=claims["sub"],
        name=str(claims.get("display_name") or claims.get("name") or claims.get("email") or ""),
        email=str(claims.get("email") or ""),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
        csrf=new_csrf(),
    )
    response = RedirectResponse(pending["next"], status_code=303)
    set_session_cookie(request, response, session)
    response.delete_cookie(LOGIN_COOKIE, path="/auth")
    log.info("signed in", extra={"sub": session.sub})
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    session = await current_session(request)
    response = RedirectResponse("/", status_code=303)
    if session is None:
        clear_session_cookie(request, response)
        return response
    await check_csrf(request, session)
    await request.app.state.keycloak.end_session(session.refresh_token)
    clear_session_cookie(request, response)
    request.state.session_cleared = True
    return response


def _back_home(request: Request) -> Response:
    response = RedirectResponse("/?signin=failed", status_code=303)
    response.delete_cookie(LOGIN_COOKIE, path="/auth")
    return response


async def current_session(request: Request) -> Session | None:
    """The signed-in user, refreshing the access token when it is about to expire; None if
    nobody is signed in or the session can no longer be refreshed."""
    cached = getattr(request.state, "session", "unset")
    if cached != "unset":
        return cached
    codec: SessionCodec = request.app.state.codec
    session = codec.decode_session(request.cookies.get(SESSION_COOKIE))
    if session is not None and session.access_expiring():
        session = await _refreshed(request, session)
    request.state.session = session
    return session


async def _refreshed(request: Request, session: Session) -> Session | None:
    if session.refresh_expired():
        request.state.session_cleared = True
        return None
    keycloak: Keycloak = request.app.state.keycloak
    try:
        tokens = await keycloak.refresh(session.refresh_token)
        keycloak.check(tokens.access_token)
    except SignInError as exc:
        log.info("session could not be refreshed", extra={"reason": str(exc)})
        request.state.session_cleared = True
        return None
    session.access_token = tokens.access_token
    session.refresh_token = tokens.refresh_token or session.refresh_token
    session.access_expires_at = tokens.access_expires_at
    session.refresh_expires_at = tokens.refresh_expires_at
    request.state.session_renewed = session
    return session


async def require_session(request: Request) -> Session:
    session = await current_session(request)
    if session is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise SignInRequiredError(target)
    return session


async def check_csrf(request: Request, session: Session) -> None:
    """Forms send csrf_token; HTMX requests may send the X-CSRF-Token header instead."""
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = str(form.get("csrf_token") or "")
    if not sent or not hmac.compare_digest(sent, session.csrf):
        raise CsrfError()


def set_session_cookie(request: Request, response: Response, session: Session) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.codec.encode_session(session),
        max_age=max(60, int(session.refresh_expires_at - time.time())),
        path="/",
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="lax",
    )
