"""Shared test fixtures for the finance-tracker test suite."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.account import Account, AccountType, AccountTerm
from app.models.budget import Budget  # noqa: F401
from app.models.category import Category
from app.models.commitment import Commitment  # noqa: F401
from app.models.reconciliation import Reconciliation  # noqa: F401
from app.models.statement import Statement, StatementLine  # noqa: F401
from app.models.transaction import Transaction
from app.models.user import User


TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db():
    """Provide a fresh in-memory SQLite database for each test."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def user(db: AsyncSession) -> User:
    """Create a test user."""
    u = User(
        id=uuid.uuid4(),
        email="test@example.com",
        password_hash="fakehash",
        display_name="Test User",
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def account(db: AsyncSession, user: User) -> Account:
    """Create a test account linked to an Akahu account."""
    a = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        name="ANZ Everyday",
        account_type=AccountType.CHECKING,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        current_balance=Decimal("1000.00"),
        institution="ANZ",
        term=AccountTerm.SHORT,
        akahu_id="acc_test_123",
    )
    db.add(a)
    await db.flush()
    return a


@pytest_asyncio.fixture
async def unlinked_account(db: AsyncSession, user: User) -> Account:
    """Create a test account NOT linked to Akahu."""
    a = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Cash Account",
        account_type=AccountType.CASH,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        current_balance=Decimal("500.00"),
        institution=None,
        term=AccountTerm.SHORT,
    )
    db.add(a)
    await db.flush()
    return a


def make_akahu_account(
    akahu_id: str = "acc_test_123",
    name: str = "ANZ Everyday",
    balance_current: float = 1234.56,
    balance_available: float | None = 1234.56,
    status: str = "ACTIVE",
    acct_type: str = "CHECKING",
    attributes: list[str] | None = None,
) -> dict:
    """Build a mock Akahu account response item."""
    bal = {"currency": "NZD", "current": balance_current}
    if balance_available is not None:
        bal["available"] = balance_available
    return {
        "_id": akahu_id,
        "name": name,
        "type": acct_type,
        "status": status,
        "balance": bal,
        "connection": {"_id": "conn_anz", "name": "ANZ", "logo": "", "connection_type": "official"},
        "formatted_account": "06-0001-0012345-00",
        "attributes": attributes or ["TRANSACTIONS", "TRANSFER_FROM", "TRANSFER_TO"],
        "refreshed": {"balance": "2026-04-11T01:00:00.000Z"},
        "_authorisation": "authorisation_test",
    }


def make_akahu_transaction(
    tx_id: str = "trans_abc123",
    akahu_account_id: str = "acc_test_123",
    amount: float = -42.50,
    description: str = "COUNTDOWN ALBANY",
    tx_date: str = "2026-04-10T00:00:00.000Z",
    updated_at: str = "2026-04-10T06:00:00.000Z",
    reference: str | None = "ref001",
    tx_type: str = "EFTPOS",
) -> dict:
    """Build a mock Akahu transaction response item."""
    tx = {
        "_id": tx_id,
        "_account": akahu_account_id,
        "_connection": "conn_anz",
        "_user": "user_test",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "type": tx_type,
        "created_at": "2026-04-10T05:00:00.000Z",
        "updated_at": updated_at,
    }
    if reference:
        tx["meta"] = {"reference": reference}
    return tx
