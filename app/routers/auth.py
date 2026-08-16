from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.templating import templates
from app.services import auth as auth_service
from app.services.categories import seed_default_categories

router = APIRouter(tags=["auth"])

AUTH_COOKIES = ("access_token", "refresh_token")
# Where a signed-out user is sent, and where we refuse to send them *back* to
# after signing in (a ``next`` pointing here would just loop).
_AUTH_PATHS = ("/login", "/logout", "/register")


# --------------------------------------------------------------------------- #
# Session expiry: make it loud and lossless
# --------------------------------------------------------------------------- #

def safe_next(target: str | None) -> str | None:
    """Return ``target`` if it is a same-origin path we may redirect to.

    Anything that could leave the site (a scheme, a host, a protocol-relative
    ``//host``) or bounce straight back into the auth pages is dropped.
    """
    if not target:
        return None
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return None
    if "\r" in target or "\n" in target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    if parts.path.startswith(_AUTH_PATHS):
        return None
    return target


def _is_xhr(request: Request) -> bool:
    """True for htmx / fetch traffic, False for a navigation or form submit.

    Browsers stamp every fetch/XHR with ``Sec-Fetch-Mode: cors`` (or
    ``same-origin``) and navigations with ``navigate``; htmx additionally sends
    ``HX-Request`` and this app sends ``X-Requested-With`` from its fetches.
    A client that sends none of these (curl, the test client) is treated as a
    navigation, which keeps the plain 302 behaviour.
    """
    h = request.headers
    if h.get("hx-request", "").lower() == "true":
        return True
    if h.get("x-requested-with", "").lower() == "xmlhttprequest":
        return True
    mode = h.get("sec-fetch-mode", "").lower()
    return bool(mode) and mode != "navigate"


def _return_to(request: Request, xhr: bool) -> str | None:
    """The page to come back to after signing in again.

    A navigation GET can simply be replayed. A POST cannot (its body is gone
    by the time the login form is shown) and an XHR's own URL is a fragment
    endpoint, not a page — for both, the page the request came from is what
    the user wants back, and the Referer names it.
    """
    if request.method in ("GET", "HEAD") and not xhr:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return safe_next(target)
    referer = request.headers.get("referer")
    if not referer:
        return None
    parts = urlsplit(referer)
    if parts.netloc and parts.netloc != request.url.netloc:
        return None
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    return safe_next(target)


def login_url(next_: str | None = None, expired: bool = False) -> str:
    params: dict[str, str] = {}
    if next_:
        params["next"] = next_
    if expired:
        params["expired"] = "1"
    return "/login" + (f"?{urlencode(params)}" if params else "")


class _RedirectToLogin(HTTPException):
    """Raised by ``require_user`` when there is no live session.

    Navigations get a 302 to the login page carrying ``next`` (so the user
    lands back where they were) and ``expired=1`` when a session cookie was
    present, i.e. this was a lapse rather than a first visit. htmx/fetch
    callers get a 401 instead — a browser would silently follow a 302 and hand
    the script the login page as a 200 — with ``HX-Redirect`` so htmx
    navigates, and the same URL in the body for the fetch wrapper in base.html.
    """

    def __init__(self, request: Request):
        xhr = _is_xhr(request)
        expired = any(name in request.cookies for name in AUTH_COOKIES)
        url = login_url(_return_to(request, xhr), expired)
        if xhr:
            super().__init__(
                status_code=401,
                detail={"error": "session_expired", "login": url},
                headers={"HX-Redirect": url, "Cache-Control": "no-store"},
            )
        else:
            super().__init__(status_code=302, headers={"Location": url})


# --------------------------------------------------------------------------- #
# Session lookup + silent renewal
# --------------------------------------------------------------------------- #

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    token = request.cookies.get("access_token")
    if token:
        user_id = auth_service.decode_access_token(token)
        if user_id:
            return await auth_service.get_user_by_id(db, user_id)

    # Access token missing or expired: fall back to the refresh token. Its
    # first use retires it and mints a new pair, which SessionCookieMiddleware
    # attaches to whatever response the endpoint returns — that is what turns
    # a hard 30-minute cutoff into a session that slides while it is used.
    refresh = request.cookies.get("refresh_token")
    if not refresh:
        return None
    outcome = await auth_service.validate_refresh_token(db, refresh)
    if not outcome:
        return None
    if outcome.rotate:
        access = auth_service.create_access_token(str(outcome.user.id))
        new_refresh = await auth_service.create_refresh_token(db, outcome.user.id)
        # Make the rotation durable now: an endpoint that goes on to raise
        # (a 404, say) rolls the request's session back, and the browser would
        # be left holding a refresh cookie that no longer exists.
        await db.commit()
        request.state.rotated_session = (access, new_refresh)
    return outcome.user


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await get_current_user(request, db)
    if not user:
        raise _RedirectToLogin(request)
    return user


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    secure = settings.COOKIE_SECURE
    response.set_cookie(
        "access_token", access_token,
        httponly=True, samesite="lax", secure=secure,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        "refresh_token", refresh_token,
        httponly=True, samesite="lax", secure=secure,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )


def _clear_auth_cookies(response: Response) -> None:
    for name in AUTH_COOKIES:
        response.delete_cookie(name)


class SessionCookieMiddleware:
    """Attach the cookies for a session pair rotated during the request.

    ``get_current_user`` leaves a renewed (access, refresh) pair on
    ``request.state.rotated_session``. Endpoints return their own Response
    objects (templates, redirects), so FastAPI would not merge cookies set on a
    dependency's Response — they are appended here on the way out instead. A
    response that already sets or clears ``access_token`` itself (login,
    logout, password change) is left alone: it knows better.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                pair = (scope.get("state") or {}).get("rotated_session")
                if pair:
                    headers = MutableHeaders(scope=message)
                    already = any(v.startswith("access_token=") for v in headers.getlist("set-cookie"))
                    if not already:
                        carrier = Response()
                        set_auth_cookies(carrier, *pair)
                        for key, value in carrier.raw_headers:
                            if key == b"set-cookie":
                                headers.append("set-cookie", value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next_: str | None = Query(None, alias="next"),
    expired: str | None = None,
):
    return templates.TemplateResponse(request, "auth/login.html", {
        "error": None,
        "next": safe_next(next_),
        "expired": expired == "1",
    })


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next_: str | None = Form(None, alias="next"),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    next_ = safe_next(next_)

    if await auth_service.is_login_locked(db, email, ip):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Too many failed attempts. Please try again later.", "next": next_, "expired": False},
            status_code=429,
        )

    user = await auth_service.authenticate_user(db, email, password)
    if not user:
        await auth_service.record_failed_login(db, email, ip)
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Invalid email or password", "next": next_, "expired": False},
            status_code=401,
        )

    await auth_service.reset_login_attempts(db, email, ip)
    access = auth_service.create_access_token(str(user.id))
    refresh = await auth_service.create_refresh_token(db, user.id)
    response = RedirectResponse(url=next_ or "/dashboard", status_code=302)
    set_auth_cookies(response, access, refresh)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "auth/register.html", {"error": None})


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.register_user(db, email, password, display_name)
    if not user:
        return templates.TemplateResponse(request, "auth/register.html", {"error": "Email already registered"}, status_code=400)

    await seed_default_categories(db, user.id)

    access = auth_service.create_access_token(str(user.id))
    refresh = await auth_service.create_refresh_token(db, user.id)
    response = RedirectResponse(url="/dashboard", status_code=302)
    set_auth_cookies(response, access, refresh)
    return response


@router.get("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if user:
        await auth_service.revoke_all_refresh_tokens(db, user.id)
    response = RedirectResponse(url="/login", status_code=302)
    # Clearing the cookies here also stops SessionCookieMiddleware re-issuing
    # a pair that get_current_user may just have rotated.
    _clear_auth_cookies(response)
    return response


@router.get("/auth/ping", status_code=204)
async def ping(user: User = Depends(require_user)):
    """Keep-alive for pages the user works on without otherwise touching the
    server (reconciliation, mostly). Renews the session as a side effect of
    ``require_user`` while there is activity; a lapsed one answers 401 so the
    page can say so *before* a Save is lost."""
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
