"""Compartment #0 — multi-tenant isolation regression tests.

Proves that user B cannot read or mutate user A's accounts, statements, or
reconciliations through the hardened endpoints, and that the removed SQL tool
is unreachable. Endpoint tests drive the real FastAPI app with the auth/session
dependencies overridden to simulate "logged in as" a chosen user; service tests
pin the defence-in-depth scoping inside ``import_statement_lines``.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.main import app
from app.models.account import Account, AccountTerm, AccountType
from app.models.reconciliation import Reconciliation
from app.models.statement import FileType, Statement, StatementLine, StatementStatus
from app.models.transaction import Transaction
from app.models.user import User
from app.routers.auth import require_user
from app.services import import_service as import_svc


@pytest_asyncio.fixture
async def user_b(db: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email="attacker@example.com",
        password_hash="fakehash",
        display_name="User B",
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def account_b(db: AsyncSession, user_b: User) -> Account:
    a = Account(
        id=uuid.uuid4(),
        user_id=user_b.id,
        name="B Checking",
        account_type=AccountType.CHECKING,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        term=AccountTerm.SHORT,
    )
    db.add(a)
    await db.flush()
    return a


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client_as(db: AsyncSession, current_user: User) -> AsyncClient:
    """An ASGI client whose auth + db dependencies resolve to the test session
    and the given 'logged in' user. ASGITransport does not run lifespan, so the
    background scheduler never starts."""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _count(db: AsyncSession, model) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return result.scalar_one()


# --------------------------------------------------------------------------- #
# sql_tool is gone
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_sql_tool_routes_removed(db: AsyncSession, user: User):
    async with _client_as(db, user) as client:
        assert (await client.get("/sql")).status_code == 404
        assert (await client.post("/sql/execute", data={"query": "SELECT 1"})).status_code == 404


# --------------------------------------------------------------------------- #
# reconciliation IDOR
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_save_draft_foreign_account_404_and_no_write(
    db: AsyncSession, user: User, account: Account, user_b: User,
):
    form = {"statement_date": "2026-01-31", "statement_balance": "100.00"}
    async with _client_as(db, user_b) as client:
        resp = await client.post(f"/reconciliation/{account.id}/save-draft", data=form)
    assert resp.status_code == 404
    assert await _count(db, Reconciliation) == 0


@pytest.mark.asyncio
async def test_finish_foreign_account_404_and_no_write(
    db: AsyncSession, user: User, account: Account, user_b: User,
):
    form = {"statement_date": "2026-01-31", "statement_balance": "100.00"}
    async with _client_as(db, user_b) as client:
        resp = await client.post(f"/reconciliation/{account.id}/finish", data=form)
    assert resp.status_code == 404
    assert await _count(db, Reconciliation) == 0


@pytest.mark.asyncio
async def test_owner_can_save_draft(
    db: AsyncSession, user: User, account: Account,
):
    form = {"statement_date": "2026-01-31", "statement_balance": "100.00"}
    async with _client_as(db, user) as client:
        resp = await client.post(
            f"/reconciliation/{account.id}/save-draft", data=form,
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert await _count(db, Reconciliation) == 1


# --------------------------------------------------------------------------- #
# imports IDOR
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def statement_a(db: AsyncSession, user: User, account: Account) -> Statement:
    stmt = Statement(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        filename="a.csv",
        file_type=FileType.CSV,
        record_count=1,
        status=StatementStatus.PENDING,
    )
    db.add(stmt)
    await db.flush()
    line = StatementLine(
        id=uuid.uuid4(),
        statement_id=stmt.id,
        date=date(2026, 1, 15),
        amount=Decimal("42.00"),
        description="Coffee",
    )
    db.add(line)
    await db.flush()
    stmt._test_line_id = line.id  # type: ignore[attr-defined]
    return stmt


@pytest.mark.asyncio
async def test_confirm_import_foreign_statement_404(
    db: AsyncSession, user: User, account: Account, statement_a: Statement,
    user_b: User, account_b: Account,
):
    # B targets B's own account but A's statement -> statement ownership blocks.
    form = {
        "statement_id": str(statement_a.id),
        "account_id": str(account_b.id),
        "line_ids": [str(statement_a._test_line_id)],
    }
    async with _client_as(db, user_b) as client:
        resp = await client.post("/imports/confirm", data=form)
    assert resp.status_code == 404
    # No transactions created, A's statement untouched.
    assert await _count(db, Transaction) == 0
    await db.refresh(statement_a)
    assert statement_a.status == StatementStatus.PENDING


@pytest.mark.asyncio
async def test_confirm_import_foreign_account_404(
    db: AsyncSession, user: User, account: Account, statement_a: Statement,
    user_b: User,
):
    # B targets A's account directly -> account ownership blocks.
    form = {
        "statement_id": str(statement_a.id),
        "account_id": str(account.id),
        "line_ids": [str(statement_a._test_line_id)],
    }
    async with _client_as(db, user_b) as client:
        resp = await client.post("/imports/confirm", data=form)
    assert resp.status_code == 404
    assert await _count(db, Transaction) == 0


@pytest.mark.asyncio
async def test_upload_foreign_account_404(
    db: AsyncSession, account: Account, user_b: User,
):
    async with _client_as(db, user_b) as client:
        resp = await client.post(
            "/imports/upload",
            data={"account_id": str(account.id)},
            files={"file": ("x.csv", b"date,amount\n", "text/csv")},
        )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# import_statement_lines service scoping (defence in depth)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_import_statement_lines_rejects_foreign_statement(
    db: AsyncSession, user: User, statement_a: Statement,
    user_b: User, account_b: Account,
):
    result = await import_svc.import_statement_lines(
        db, user_b.id, statement_a.id, [statement_a._test_line_id], account_b.id,
    )
    assert result.imported == 0
    assert await _count(db, Transaction) == 0
    await db.refresh(statement_a)
    assert statement_a.status == StatementStatus.PENDING


@pytest.mark.asyncio
async def test_import_statement_lines_skips_lines_from_other_statements(
    db: AsyncSession, user: User, account: Account, statement_a: Statement,
):
    # A second statement owned by the same user, with its own line.
    other = Statement(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        filename="b.csv", file_type=FileType.CSV, record_count=1,
        status=StatementStatus.PENDING,
    )
    db.add(other)
    await db.flush()
    foreign_line = StatementLine(
        id=uuid.uuid4(), statement_id=other.id,
        date=date(2026, 2, 1), amount=Decimal("9.99"), description="Other",
    )
    db.add(foreign_line)
    await db.flush()

    # Importing statement_a but passing a line that belongs to `other`.
    result = await import_svc.import_statement_lines(
        db, user.id, statement_a.id, [foreign_line.id], account.id,
    )
    assert result.imported == 0
    assert await _count(db, Transaction) == 0


@pytest.mark.asyncio
async def test_import_statement_lines_owner_happy_path(
    db: AsyncSession, user: User, account: Account, statement_a: Statement,
):
    result = await import_svc.import_statement_lines(
        db, user.id, statement_a.id, [statement_a._test_line_id], account.id,
    )
    assert result.imported == 1
    assert await _count(db, Transaction) == 1
    await db.refresh(statement_a)
    assert statement_a.status == StatementStatus.IMPORTED
