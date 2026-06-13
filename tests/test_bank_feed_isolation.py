"""Compartment #2b — bank-feed tenancy isolation regression tests.

The Akahu integration is single-tenant: one global token connecting one real
person's bank. Before this compartment, GET /bank-feeds rendered those external
accounts and bank-reported balances to *every* logged-in user, and any user
could link/sync them. These tests pin the containment:

  - only the configured connection owner (AKAHU_OWNER_EMAIL) ever triggers an
    external fetch or sees external accounts/balances;
  - link / unlink / sync / sync-transactions return 404 for non-owners;
  - unset owner => fail closed (no one sees the external surface).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.user import User
from app.routers import bank_feeds as bf
from app.routers.auth import require_user
from app.services import akahu

from .conftest import make_akahu_account


OWNER_EMAIL = "owner@example.com"


@pytest_asyncio.fixture
async def owner_user(db: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email=OWNER_EMAIL,
        password_hash="fakehash",
        display_name="Owner",
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def other_user(db: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email="stranger@example.com",
        password_hash="fakehash",
        display_name="Stranger",
    )
    db.add(u)
    await db.flush()
    return u


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def _akahu_env(monkeypatch):
    """Akahu configured + owner set to OWNER_EMAIL, with fetch_accounts mocked."""
    monkeypatch.setattr(settings, "AKAHU_APP_TOKEN", "app_tok")
    monkeypatch.setattr(settings, "AKAHU_USER_TOKEN", "user_tok")
    monkeypatch.setattr(settings, "AKAHU_OWNER_EMAIL", OWNER_EMAIL)
    fetch = AsyncMock(return_value=[make_akahu_account()])
    monkeypatch.setattr(bf, "akahu_fetch_accounts", fetch)
    return fetch


def _client_as(db: AsyncSession, current_user: User) -> AsyncClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# is_owner unit behaviour
# --------------------------------------------------------------------------- #

class _U:
    def __init__(self, email):
        self.email = email


def test_is_owner_matches_normalised(monkeypatch):
    monkeypatch.setattr(settings, "AKAHU_OWNER_EMAIL", "  Owner@Example.COM ")
    assert akahu.is_owner(_U("owner@example.com")) is True
    assert akahu.is_owner(_U("OWNER@EXAMPLE.COM")) is True
    assert akahu.is_owner(_U("someone@else.com")) is False


def test_is_owner_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "AKAHU_OWNER_EMAIL", "")
    assert akahu.is_owner(_U("owner@example.com")) is False
    assert akahu.is_owner(_U("")) is False


# --------------------------------------------------------------------------- #
# GET /bank-feeds page rendering
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_non_owner_page_hides_external_accounts(
    db: AsyncSession, owner_user: User, other_user: User, _akahu_env
):
    async with _client_as(db, other_user) as client:
        resp = await client.get("/bank-feeds")
    assert resp.status_code == 200
    body = resp.text
    # No external bank data leaks to a non-owner.
    assert "ANZ Everyday" not in body
    assert "1234.56" not in body
    # Restricted card is shown instead.
    assert "managed by the connection owner" in body
    # And no external fetch was ever attempted.
    _akahu_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_page_shows_external_accounts(
    db: AsyncSession, owner_user: User, _akahu_env
):
    async with _client_as(db, owner_user) as client:
        resp = await client.get("/bank-feeds")
    assert resp.status_code == 200
    assert "ANZ Everyday" in resp.text
    _akahu_env.assert_awaited()


@pytest.mark.asyncio
async def test_page_fails_closed_when_owner_unset(
    db: AsyncSession, owner_user: User, monkeypatch
):
    monkeypatch.setattr(settings, "AKAHU_APP_TOKEN", "app_tok")
    monkeypatch.setattr(settings, "AKAHU_USER_TOKEN", "user_tok")
    monkeypatch.setattr(settings, "AKAHU_OWNER_EMAIL", "")
    fetch = AsyncMock(return_value=[make_akahu_account()])
    monkeypatch.setattr(bf, "akahu_fetch_accounts", fetch)

    # Even the would-be owner sees nothing while AKAHU_OWNER_EMAIL is unset.
    async with _client_as(db, owner_user) as client:
        resp = await client.get("/bank-feeds")
    assert resp.status_code == 200
    assert "ANZ Everyday" not in resp.text
    assert "managed by the connection owner" in resp.text
    fetch.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Mutating routes are owner-only (404 for non-owners)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_non_owner_link_404(db: AsyncSession, other_user: User, _akahu_env):
    form = {"akahu_id": "acc_test_123", "account_id": str(uuid.uuid4())}
    async with _client_as(db, other_user) as client:
        resp = await client.post("/bank-feeds/link", data=form)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_owner_unlink_404(db: AsyncSession, other_user: User, _akahu_env):
    async with _client_as(db, other_user) as client:
        resp = await client.post(f"/bank-feeds/unlink/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_owner_sync_404(db: AsyncSession, other_user: User, _akahu_env):
    async with _client_as(db, other_user) as client:
        resp = await client.post("/bank-feeds/sync")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_owner_sync_transactions_404(
    db: AsyncSession, other_user: User, _akahu_env
):
    form = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    async with _client_as(db, other_user) as client:
        resp = await client.post(
            f"/bank-feeds/sync-transactions/{uuid.uuid4()}", data=form
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_link_404_when_owner_unset(db: AsyncSession, owner_user: User, monkeypatch):
    monkeypatch.setattr(settings, "AKAHU_APP_TOKEN", "app_tok")
    monkeypatch.setattr(settings, "AKAHU_USER_TOKEN", "user_tok")
    monkeypatch.setattr(settings, "AKAHU_OWNER_EMAIL", "")
    form = {"akahu_id": "acc_test_123", "account_id": str(uuid.uuid4())}
    async with _client_as(db, owner_user) as client:
        resp = await client.post("/bank-feeds/link", data=form)
    assert resp.status_code == 404
