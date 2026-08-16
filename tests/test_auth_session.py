"""Session lifetime and expiry behaviour.

Covers the fix for the silent timeout: the access token expires after
ACCESS_TOKEN_EXPIRE_MINUTES, after which the refresh cookie must renew the
session (rotating the pair) instead of being consumed once and then bouncing
every request to /login; and when a session really is dead, the bounce must
be loud — a login URL that returns the user to where they were, an ``expired``
flag for the login page, and a 401 (+ HX-Redirect) rather than a 302 for
htmx/fetch traffic that would otherwise swallow the redirect.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, HTTPException
from jose import jwt
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.user import RefreshToken, User
from app.routers import auth as auth_router
from app.routers.auth import safe_next
from app.services import auth as auth_service

EMAIL = "sess@example.com"
PASSWORD = "correct-horse"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _expired_access_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) - timedelta(minutes=1)
    return jwt.encode({"sub": user_id, "exp": exp, "type": "access"},
                      settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _cookies(**values: str) -> dict[str, str]:
    """An explicit Cookie header. httpx's jar leaves an existing header alone,
    so each request carries exactly the cookies the test says it does."""
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in values.items())}


def _set_cookies(r: httpx.Response) -> dict[str, str]:
    """name -> value for every Set-Cookie on the response (deleted cookies -> '')."""
    out: dict[str, str] = {}
    for header in r.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        out[name.strip()] = rest.split(";", 1)[0].strip('"')
    return out


def _login_query(location: str) -> dict[str, list[str]]:
    parts = urlsplit(location)
    assert parts.path == "/login"
    return parse_qs(parts.query)


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(auth_router.settings, "COOKIE_SECURE", False)

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def session_user(db) -> User:
    user = User(email=EMAIL, password_hash=auth_service.hash_password(PASSWORD), display_name="Sess")
    db.add(user)
    await db.flush()
    return user


async def _login(client: httpx.AsyncClient) -> tuple[str, str]:
    r = await client.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"
    cookies = _set_cookies(r)
    return cookies["access_token"], cookies["refresh_token"]


# --------------------------------------------------------------------------- #
# A. renewal + rotation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_expired_access_token_is_renewed_and_session_slides(client, session_user):
    _, refresh = await _login(client)
    stale = _expired_access_token(str(session_user.id))

    # First request after the access token lapsed: served, and a new pair issued.
    r1 = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r1.status_code == 200
    issued = _set_cookies(r1)
    assert set(issued) == {"access_token", "refresh_token"}
    assert issued["refresh_token"] != refresh
    assert auth_service.decode_access_token(issued["access_token"]) == str(session_user.id)
    for header in r1.headers.get_list("set-cookie"):
        assert "HttpOnly" in header and "SameSite=lax" in header.replace("Lax", "lax")

    # With the new pair, requests keep working and nothing is re-issued.
    r2 = await client.get("/reconciliation", headers=_cookies(**issued))
    assert r2.status_code == 200
    assert "set-cookie" not in r2.headers

    # ...and once *that* access token lapses, the new refresh token renews again.
    r3 = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=issued["refresh_token"]))
    assert r3.status_code == 200
    assert _set_cookies(r3)["refresh_token"] not in (refresh, issued["refresh_token"])


@pytest.mark.asyncio
async def test_retired_refresh_token_works_inside_grace_only(client, session_user, monkeypatch):
    _, refresh = await _login(client)
    stale = _expired_access_token(str(session_user.id))

    r1 = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r1.status_code == 200 and "set-cookie" in r1.headers

    # A request that was already in flight with the old cookie is still served,
    # but the pair minted by the first presentation stands (nothing re-issued).
    r2 = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r2.status_code == 200
    assert "set-cookie" not in r2.headers

    # Outside the grace window the retired token is dead.
    monkeypatch.setattr(auth_service, "REFRESH_REUSE_GRACE", timedelta(seconds=-1))
    r3 = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r3.status_code == 302
    assert _login_query(r3.headers["location"])["expired"] == ["1"]


@pytest.mark.asyncio
async def test_rotation_survives_an_endpoint_that_raises(client, session_user, db):
    """A 404 raised inside the endpoint rolls the request's session back; the
    pair we already put in the browser's cookies must still exist."""
    _, refresh = await _login(client)
    stale = _expired_access_token(str(session_user.id))

    async def _boom(user: User = Depends(auth_router.require_user)):
        raise HTTPException(status_code=404)

    app.add_api_route("/__test_boom", _boom)
    try:
        r = await client.get("/__test_boom", headers=_cookies(access_token=stale, refresh_token=refresh))
    finally:
        app.router.routes[:] = [rt for rt in app.router.routes if getattr(rt, "path", None) != "/__test_boom"]
    assert r.status_code == 404
    issued = _set_cookies(r)
    assert issued["refresh_token"] != refresh

    live = (await db.execute(
        select(RefreshToken).where(RefreshToken.user_id == session_user.id, RefreshToken.is_revoked.is_(False))
    )).scalars().all()
    assert len(live) == 1
    assert live[0].token_hash == auth_service._hash_refresh(issued["refresh_token"])


@pytest.mark.asyncio
async def test_revoked_refresh_token_is_not_treated_as_in_grace(client, session_user, db):
    """Outright revocation (logout / password change) keeps the token's far-off
    expiry, so it must never satisfy the grace arm."""
    _, refresh = await _login(client)
    await auth_service.revoke_all_refresh_tokens(db, session_user.id)
    await db.flush()
    stale = _expired_access_token(str(session_user.id))
    r = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r.status_code == 302


@pytest.mark.asyncio
async def test_logout_after_lapse_clears_rather_than_reissues(client, session_user, db):
    _, refresh = await _login(client)
    stale = _expired_access_token(str(session_user.id))
    r = await client.get("/logout", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r.status_code == 302 and r.headers["location"] == "/login"
    cleared = _set_cookies(r)
    assert cleared["access_token"] == "" and cleared["refresh_token"] == ""
    assert sum(1 for h in r.headers.get_list("set-cookie") if h.startswith("access_token=")) == 1
    live = (await db.execute(
        select(RefreshToken).where(RefreshToken.user_id == session_user.id, RefreshToken.is_revoked.is_(False))
    )).scalars().all()
    assert live == []


@pytest.mark.asyncio
async def test_logout_also_kills_a_token_inside_its_reuse_grace(client, session_user):
    _, refresh = await _login(client)
    stale = _expired_access_token(str(session_user.id))
    rotated = _set_cookies(await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh)))
    # Sign out with the new pair; the retired-but-in-grace old token must die too.
    await client.get("/logout", headers=_cookies(**rotated))
    r = await client.get("/reconciliation", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert r.status_code == 302


@pytest.mark.asyncio
async def test_password_change_after_lapse_keeps_its_own_cookies(client, session_user, db):
    """The handler revokes everything and sets a fresh pair itself; the pair
    rotated on the way in (now revoked) must not be appended over the top."""
    _, refresh = await _login(client)
    stale = _expired_access_token(str(session_user.id))
    r = await client.post(
        "/profile/password",
        data={"current_password": PASSWORD, "new_password": "new-horse-battery", "confirm_password": "new-horse-battery"},
        headers=_cookies(access_token=stale, refresh_token=refresh),
    )
    assert r.status_code == 200
    headers = r.headers.get_list("set-cookie")
    assert sum(1 for h in headers if h.startswith("access_token=")) == 1
    assert sum(1 for h in headers if h.startswith("refresh_token=")) == 1
    issued = _set_cookies(r)
    row = (await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == auth_service._hash_refresh(issued["refresh_token"]))
    )).scalar_one()
    assert row.is_revoked is False


# --------------------------------------------------------------------------- #
# B. a dead session is loud and lossless
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dead_session_navigation_redirects_to_login_with_next_and_expired(client, session_user):
    stale = _expired_access_token(str(session_user.id))
    page = "/reconciliation/11111111-1111-1111-1111-111111111111?statement_date=2026-01-31&statement_balance=100.00"
    r = await client.get(page, headers=_cookies(access_token=stale, refresh_token="not-a-real-token"))
    assert r.status_code == 302
    q = _login_query(r.headers["location"])
    assert q["next"] == [page]
    assert q["expired"] == ["1"]


@pytest.mark.asyncio
async def test_first_visit_redirects_to_login_without_expired_flag(client):
    r = await client.get("/reconciliation")
    assert r.status_code == 302
    q = _login_query(r.headers["location"])
    assert q["next"] == ["/reconciliation"]
    assert "expired" not in q


@pytest.mark.asyncio
async def test_dead_session_post_returns_to_the_referring_page(client, session_user):
    """A POST can't be replayed after login; the Referer names the page to go back to."""
    stale = _expired_access_token(str(session_user.id))
    page = "/reconciliation/11111111-1111-1111-1111-111111111111?statement_date=2026-01-31&statement_balance=100.00"
    r = await client.post(
        "/reconciliation/11111111-1111-1111-1111-111111111111/save-draft",
        data={"statement_date": "2026-01-31", "statement_balance": "100.00"},
        headers={**_cookies(access_token=stale, refresh_token="dead"), "Referer": f"http://test{page}"},
    )
    assert r.status_code == 302
    q = _login_query(r.headers["location"])
    assert q["next"] == [page]
    assert q["expired"] == ["1"]


@pytest.mark.asyncio
async def test_dead_session_post_ignores_cross_site_referer(client, session_user):
    stale = _expired_access_token(str(session_user.id))
    r = await client.post(
        "/reconciliation/11111111-1111-1111-1111-111111111111/save-draft",
        data={"statement_date": "2026-01-31", "statement_balance": "100.00"},
        headers={**_cookies(access_token=stale, refresh_token="dead"), "Referer": "https://evil.example/reconciliation"},
    )
    assert r.status_code == 302
    assert "next" not in _login_query(r.headers["location"])


@pytest.mark.parametrize("marker", [
    {"HX-Request": "true"},
    {"X-Requested-With": "XMLHttpRequest"},
    {"Sec-Fetch-Mode": "cors"},
])
@pytest.mark.asyncio
async def test_dead_session_xhr_gets_401_with_hx_redirect(client, session_user, marker):
    stale = _expired_access_token(str(session_user.id))
    page = "/transactions?account=abc"
    r = await client.get(
        "/transactions/11111111-1111-1111-1111-111111111111/detail",
        headers={**_cookies(access_token=stale, refresh_token="dead"), "Referer": f"http://test{page}", **marker},
    )
    assert r.status_code == 401
    q = _login_query(r.headers["hx-redirect"])
    assert q["next"] == [page]          # the page, not the fragment endpoint
    assert q["expired"] == ["1"]
    assert r.headers["cache-control"] == "no-store"
    body = r.json()["detail"]
    assert body["error"] == "session_expired"
    assert body["login"] == r.headers["hx-redirect"]


@pytest.mark.asyncio
async def test_navigation_marker_still_gets_a_302(client, session_user):
    stale = _expired_access_token(str(session_user.id))
    r = await client.get("/reconciliation",
                         headers={**_cookies(access_token=stale, refresh_token="dead"), "Sec-Fetch-Mode": "navigate"})
    assert r.status_code == 302


@pytest.mark.asyncio
async def test_login_page_shows_expired_notice_and_carries_next(client):
    r = await client.get("/login?next=%2Freconciliation%3Fx%3D1&expired=1")
    assert r.status_code == 200
    assert "Your session expired" in r.text
    assert 'name="next" value="/reconciliation?x=1"' in r.text

    plain = await client.get("/login")
    assert "Your session expired" not in plain.text
    assert 'name="next"' not in plain.text


@pytest.mark.parametrize("next_, expect", [
    ("/reconciliation/abc?statement_date=2026-01-31&statement_balance=1", "/reconciliation/abc?statement_date=2026-01-31&statement_balance=1"),
    ("/dashboard", "/dashboard"),
    ("https://evil.example/x", "/dashboard"),
    ("//evil.example/x", "/dashboard"),
    ("/\\evil.example", "/dashboard"),
    ("javascript:alert(1)", "/dashboard"),
    ("/login?next=/x", "/dashboard"),
    ("/logout", "/dashboard"),
    ("", "/dashboard"),
])
@pytest.mark.asyncio
async def test_login_returns_to_safe_next_only(client, session_user, next_, expect):
    r = await client.post("/login", data={"email": EMAIL, "password": PASSWORD, "next": next_})
    assert r.status_code == 302
    assert r.headers["location"] == expect


@pytest.mark.asyncio
async def test_failed_login_keeps_next_for_the_retry(client, session_user):
    r = await client.post("/login", data={"email": EMAIL, "password": "wrong", "next": "/reconciliation"})
    assert r.status_code == 401
    assert 'name="next" value="/reconciliation"' in r.text


def test_safe_next_rejects_anything_that_leaves_the_site():
    assert safe_next("/reconciliation?x=1") == "/reconciliation?x=1"
    for bad in ("", None, "reconciliation", "https://a/b", "//a/b", "/\\a", "/x\r\nSet-Cookie: a=b", "/login", "/register?x"):
        assert safe_next(bad) is None, bad


# --------------------------------------------------------------------------- #
# keep-alive
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_ping_renews_a_lapsed_access_token_and_reports_a_dead_session(client, session_user):
    access, refresh = await _login(client)
    ok = await client.get("/auth/ping", headers=_cookies(access_token=access, refresh_token=refresh))
    assert ok.status_code == 204 and "set-cookie" not in ok.headers

    stale = _expired_access_token(str(session_user.id))
    renewed = await client.get("/auth/ping", headers=_cookies(access_token=stale, refresh_token=refresh))
    assert renewed.status_code == 204
    assert set(_set_cookies(renewed)) == {"access_token", "refresh_token"}

    dead = await client.get("/auth/ping", headers={**_cookies(access_token=stale, refresh_token="dead"),
                                                   "X-Requested-With": "XMLHttpRequest",
                                                   "Referer": "http://test/reconciliation/abc?statement_date=2026-01-31&statement_balance=1"})
    assert dead.status_code == 401
    assert _login_query(dead.headers["hx-redirect"])["next"] == ["/reconciliation/abc?statement_date=2026-01-31&statement_balance=1"]
