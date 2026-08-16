"""Statement helper (merged recon-helper) and the reconcile screen's split view.

Drives the real FastAPI app with auth/db overridden, the same way
test_tenant_isolation does. The helper itself runs in the browser; what the
server owns is: the route exists and isn't swallowed by /{account_id}, the
partial mounts once per page in the right mode, and the split view is wired
to the account being reconciled.
"""
from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.main import app
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.user import User
from app.routers.auth import require_user


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client_as(db: AsyncSession, current_user: User) -> AsyncClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_helper_page_renders_standalone(db: AsyncSession, user: User):
    """/reconciliation/helper is a real page, not a UUID parse failure on /{account_id}."""
    async with _client_as(db, user) as client:
        resp = await client.get("/reconciliation/helper")
    assert resp.status_code == 200
    body = resp.text
    assert body.count("<div data-recon-helper") == 1
    assert body.count("window.reconHelper = function") == 1   # partial included exactly once
    assert 'data-embedded=""' in body                          # standalone mode
    assert 'data-rh="followDiff"' not in body                  # split-only control absent
    assert 'data-rh="hideChecked"' in body                     # Hide ticked travels with the helper
    assert "Statement Helper" in body


@pytest.mark.asyncio
async def test_helper_page_scopes_session_to_own_account(db: AsyncSession, user: User, account: Account):
    async with _client_as(db, user) as client:
        resp = await client.get(f"/reconciliation/helper?account_id={account.id}")
    assert resp.status_code == 200
    assert f'data-scope="{account.id}"' in resp.text
    assert account.name in resp.text


@pytest.mark.asyncio
async def test_helper_page_ignores_foreign_or_bad_account_id(db: AsyncSession, user: User):
    """The id only picks a saved-session scope; an unknown or malformed one is
    dropped rather than 404'd, since nothing is read on its behalf."""
    async with _client_as(db, user) as client:
        foreign = await client.get(f"/reconciliation/helper?account_id={uuid.uuid4()}")
        junk = await client.get("/reconciliation/helper?account_id=not-a-uuid")
    assert foreign.status_code == 200 and 'data-scope=""' in foreign.text
    assert junk.status_code == 200 and 'data-scope=""' in junk.text


@pytest.mark.asyncio
async def test_reconcile_screen_embeds_helper_for_split_view(db: AsyncSession, user: User, account: Account):
    async with _client_as(db, user) as client:
        resp = await client.get(
            f"/reconciliation/{account.id}",
            params={"statement_date": "2026-07-31", "statement_balance": "2193.09"},
        )
    assert resp.status_code == 200
    body = resp.text
    # Split-view plumbing
    assert "Alpine.store('reconSplit'" in body
    assert 'x-ref="helperPane"' in body
    assert "recon-split-divider" in body
    # Helper mounted once, embedded, scoped to this account, cross-checking this balance
    assert body.count("<div data-recon-helper") == 1
    assert body.count("window.reconHelper = function") == 1
    assert 'data-embedded="1"' in body
    assert f'data-scope="{account.id}"' in body
    assert 'data-expected-balance="2193.09"' in body
    assert 'data-rh="followDiff"' in body
    # Existing behaviour still on the page
    assert "Finish Reconciliation" in body and 'id="txBody"' in body


_HIDE_TICKED_LABEL = re.compile(r"<input[^>]*type=\"checkbox\"[^>]*>\s*Hide ticked\s*</label>")


@pytest.mark.asyncio
async def test_reconcile_screen_has_hide_ticked_on_both_sides(db: AsyncSession, user: User, account: Account):
    """Each pane of the split view carries its own Hide ticked switch, worded the
    same: the transaction list hides rows already ticked (Alpine state,
    remembered in localStorage like the sort choice) and the statement helper
    hides ticked lines (its own state, saved with the session). Neither is a
    form field, so nothing new is posted on Save Draft / Finish."""
    tx = Transaction(
        user_id=user.id, account_id=account.id,
        date=date(2026, 7, 19), amount=Decimal("-42.50"),
        description="COUNTDOWN ALBANY", is_cleared=False,
    )
    db.add(tx)
    await db.flush()

    async with _client_as(db, user) as client:
        resp = await client.get(
            f"/reconciliation/{account.id}",
            params={"statement_date": "2026-07-31", "statement_balance": "2193.09"},
        )
    assert resp.status_code == 200
    body = resp.text

    # Transaction-list side: switch, persistence, per-row hide, and the
    # "everything is hidden" notice.
    assert 'x-model="hideTicked"' in body
    assert "localStorage.getItem('reconcile-hide-ticked')" in body
    assert "localStorage.setItem('reconcile-hide-ticked'" in body
    assert f"x-show=\"!isHidden('{tx.id}')\"" in body
    assert 'x-show="allHidden"' in body
    assert "txCount: 1," in body
    # Statement-helper side keeps its own switch inside the split pane.
    assert 'data-rh="hideChecked"' in body
    # Both read identically, and neither would be submitted with the form.
    labels = _HIDE_TICKED_LABEL.findall(body)
    assert len(labels) == 2, labels
    assert all("name=" not in lbl for lbl in labels)


@pytest.mark.asyncio
async def test_reconciliation_index_links_to_helper(db: AsyncSession, user: User, account: Account):
    async with _client_as(db, user) as client:
        resp = await client.get("/reconciliation")
    assert resp.status_code == 200
    assert 'href="/reconciliation/helper"' in resp.text
