"""Tests for compartment #0 auth hardening.

Pins three guarantees:
  * the app refuses to boot with a default / too-short SECRET_KEY,
  * session cookies carry Secure according to COOKIE_SECURE,
  * repeated failed logins lock the (email, ip) pair without enumerating users.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import Response

from app.config import (
    DEFAULT_SECRET_KEY,
    MIN_SECRET_KEY_LENGTH,
    Settings,
    validate_security_config,
)
from app.database import get_db
from app.main import app
from app.models.login_attempt import LoginAttempt
from app.models.user import User
from app.routers.auth import _set_auth_cookies
from app.services import auth as auth_service


# --- SECRET_KEY boot validation --------------------------------------------

def test_default_secret_key_refused():
    s = Settings(SECRET_KEY=DEFAULT_SECRET_KEY)
    with pytest.raises(RuntimeError):
        validate_security_config(s)


def test_short_secret_key_refused():
    s = Settings(SECRET_KEY="x" * (MIN_SECRET_KEY_LENGTH - 1))
    with pytest.raises(RuntimeError):
        validate_security_config(s)


def test_strong_secret_key_accepted():
    s = Settings(SECRET_KEY="z" * MIN_SECRET_KEY_LENGTH)
    validate_security_config(s)  # does not raise


# --- Cookie Secure flag -----------------------------------------------------

def test_auth_cookies_secure_when_enabled(monkeypatch):
    monkeypatch.setattr(auth_service.settings, "COOKIE_SECURE", True)
    from app.routers import auth as auth_router
    monkeypatch.setattr(auth_router.settings, "COOKIE_SECURE", True)
    resp = Response()
    _set_auth_cookies(resp, "acc", "ref")
    cookies = resp.headers.getlist("set-cookie")
    assert all("Secure" in c for c in cookies)


def test_auth_cookies_not_secure_when_disabled(monkeypatch):
    from app.routers import auth as auth_router
    monkeypatch.setattr(auth_router.settings, "COOKIE_SECURE", False)
    resp = Response()
    _set_auth_cookies(resp, "acc", "ref")
    cookies = resp.headers.getlist("set-cookie")
    assert all("Secure" not in c for c in cookies)


# --- Rate-limit service -----------------------------------------------------

@pytest.mark.asyncio
async def test_not_locked_initially(db):
    assert await auth_service.is_login_locked(db, "a@b.com", "1.2.3.4") is False


@pytest.mark.asyncio
async def test_locks_after_threshold(db):
    for _ in range(auth_service.LOGIN_MAX_ATTEMPTS):
        await auth_service.record_failed_login(db, "a@b.com", "1.2.3.4")
    assert await auth_service.is_login_locked(db, "a@b.com", "1.2.3.4") is True


@pytest.mark.asyncio
async def test_below_threshold_not_locked(db):
    for _ in range(auth_service.LOGIN_MAX_ATTEMPTS - 1):
        await auth_service.record_failed_login(db, "a@b.com", "1.2.3.4")
    assert await auth_service.is_login_locked(db, "a@b.com", "1.2.3.4") is False


@pytest.mark.asyncio
async def test_reset_clears_lock(db):
    for _ in range(auth_service.LOGIN_MAX_ATTEMPTS):
        await auth_service.record_failed_login(db, "a@b.com", "1.2.3.4")
    await auth_service.reset_login_attempts(db, "a@b.com", "1.2.3.4")
    assert await auth_service.is_login_locked(db, "a@b.com", "1.2.3.4") is False


@pytest.mark.asyncio
async def test_lock_is_scoped_to_ip(db):
    for _ in range(auth_service.LOGIN_MAX_ATTEMPTS):
        await auth_service.record_failed_login(db, "a@b.com", "1.2.3.4")
    # A different host for the same account is unaffected — an attacker on one
    # IP cannot lock the real user out.
    assert await auth_service.is_login_locked(db, "a@b.com", "9.9.9.9") is False


@pytest.mark.asyncio
async def test_old_failures_fall_out_of_window(db):
    stale = datetime.now(timezone.utc) - auth_service.LOGIN_WINDOW - timedelta(minutes=1)
    for _ in range(auth_service.LOGIN_MAX_ATTEMPTS):
        db.add(LoginAttempt(email="a@b.com", ip="1.2.3.4", successful=False, created_at=stale))
    await db.flush()
    assert await auth_service.is_login_locked(db, "a@b.com", "1.2.3.4") is False


# --- Login endpoint lockout -------------------------------------------------

@pytest.mark.asyncio
async def test_login_endpoint_locks_out(db, monkeypatch):
    from app.routers import auth as auth_router
    monkeypatch.setattr(auth_router.settings, "COOKIE_SECURE", False)

    user = User(
        email="real@example.com",
        password_hash=auth_service.hash_password("correct-horse"),
        display_name="Real",
    )
    db.add(user)
    await db.flush()

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(auth_service.LOGIN_MAX_ATTEMPTS):
                r = await client.post(
                    "/login",
                    data={"email": "real@example.com", "password": "wrong"},
                )
                assert r.status_code == 401
            # Now locked — even the correct password is rejected generically.
            r = await client.post(
                "/login",
                data={"email": "real@example.com", "password": "correct-horse"},
            )
            assert r.status_code == 429
            assert "real@example.com" not in r.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_success_resets_counter(db, monkeypatch):
    from app.routers import auth as auth_router
    monkeypatch.setattr(auth_router.settings, "COOKIE_SECURE", False)

    user = User(
        email="real@example.com",
        password_hash=auth_service.hash_password("correct-horse"),
        display_name="Real",
    )
    db.add(user)
    await db.flush()

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            for _ in range(auth_service.LOGIN_MAX_ATTEMPTS - 1):
                r = await client.post(
                    "/login", data={"email": "real@example.com", "password": "wrong"}
                )
                assert r.status_code == 401
            r = await client.post(
                "/login", data={"email": "real@example.com", "password": "correct-horse"}
            )
            assert r.status_code == 302
            assert not await auth_service.is_login_locked(db, "real@example.com", "testclient")
    finally:
        app.dependency_overrides.clear()
