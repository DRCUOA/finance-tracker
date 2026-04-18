"""Tests for the per-account reference dedupe index and manual-source tagging.

Covers the interim fix that:
  * tags new manual transactions with ``source='manual'``
  * narrows the partial unique index ``ix_transactions_account_reference_dedupe``
    to ``WHERE reference IS NOT NULL AND source IS DISTINCT FROM 'manual'``
  * surfaces dedupe-index violations as a 409 (modal) or a redirect with
    ``?error=duplicate_reference`` (form), without swallowing unrelated
    IntegrityErrors.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType, AccountTerm
from app.models.transaction import Transaction
from app.routers import transactions as tx_router
from app.services import transactions as tx_svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dedupe_index(db: AsyncSession):
    """Create the partial unique index in the SQLite test DB.

    The index is normally installed by Alembic migration 015. The test
    fixture builds the schema via ``Base.metadata.create_all`` which
    doesn't know about Alembic-managed indexes, so we emit it manually
    here to mirror production-like behaviour.
    """
    await db.execute(
        text(
            "CREATE UNIQUE INDEX ix_transactions_account_reference_dedupe "
            "ON transactions (account_id, reference) "
            "WHERE reference IS NOT NULL AND source IS DISTINCT FROM 'manual'"
        )
    )
    await db.flush()
    yield


@pytest_asyncio.fixture
async def second_account(db: AsyncSession, user) -> Account:
    """Second account on the same user, for cross-account move tests."""
    a = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Second account",
        account_type=AccountType.CHECKING,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        current_balance=Decimal("0.00"),
        institution=None,
        term=AccountTerm.SHORT,
    )
    db.add(a)
    await db.flush()
    return a


# ---------------------------------------------------------------------------
# 1. Manual creates set source='manual'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestManualSourceTagging:
    async def test_create_transaction_sets_source_manual(self, db, user, account):
        tx = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-12.34"), "Coffee",
            reference="Manual",
        )
        assert tx.source == tx_svc.MANUAL_SOURCE == "manual"

    async def test_create_transaction_without_reference_still_tagged(
        self, db, user, account,
    ):
        tx = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-1.00"), "Bus fare",
        )
        assert tx.source == "manual"
        assert tx.reference is None


# ---------------------------------------------------------------------------
# 2. Manual rows with the same reference can coexist / be moved between accts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestManualDedupeExempt:
    async def test_two_manual_rows_same_reference_same_account_allowed(
        self, db, user, account, dedupe_index,
    ):
        await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-1.00"), "First", reference="Manual",
        )
        await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-2.00"), "Second", reference="Manual",
            force=True,
        )
        result = await db.execute(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.reference == "Manual",
            )
        )
        assert len(result.scalars().all()) == 2

    async def test_move_manual_to_account_with_same_reference_allowed(
        self, db, user, account, second_account, dedupe_index,
    ):
        await tx_svc.create_transaction(
            db, user.id, second_account.id,
            date(2026, 4, 18), Decimal("-9.99"), "Existing on target",
            reference="Manual",
        )
        moving = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-5.55"), "Moving",
            reference="Manual", force=True,
        )

        moving.account_id = second_account.id
        await db.flush()

        await db.refresh(moving)
        assert moving.account_id == second_account.id


# ---------------------------------------------------------------------------
# 3. NULL-source rows are STILL covered by the unique index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestNullSourceStillCovered:
    async def test_two_null_source_same_reference_same_account_blocked(
        self, db, user, account, dedupe_index,
    ):
        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="OFX line 1", reference="REF-001",
            source=None,
        ))
        await db.flush()

        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-2.00"),
            description="OFX line 2 same ref", reference="REF-001",
            source=None,
        ))
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_akahu_source_same_reference_same_account_blocked(
        self, db, user, account, dedupe_index,
    ):
        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="Akahu A", reference="AKAHU-XYZ",
            source="akahu",
        ))
        await db.flush()

        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-2.00"),
            description="Akahu duplicate", reference="AKAHU-XYZ",
            source="akahu",
        ))
        with pytest.raises(IntegrityError):
            await db.flush()


# ---------------------------------------------------------------------------
# 4. Route-level error handling for the dedupe constraint
# ---------------------------------------------------------------------------


def _make_integrity_error(message: str) -> IntegrityError:
    """Build a standalone IntegrityError whose ``.orig`` stringifies to message."""
    return IntegrityError(statement="UPDATE transactions ...", params=None, orig=Exception(message))


class TestIsDedupeViolation:
    def test_identifies_dedupe_index_by_name(self):
        exc = _make_integrity_error(
            'duplicate key value violates unique constraint '
            '"ix_transactions_account_reference_dedupe"'
        )
        assert tx_router._is_dedupe_violation(exc) is True

    def test_does_not_match_unrelated_constraint(self):
        exc = _make_integrity_error(
            'null value in column "user_id" violates not-null constraint'
        )
        assert tx_router._is_dedupe_violation(exc) is False


@pytest.mark.asyncio
class TestModalReturns409OnDedupeViolation:
    async def test_returns_409_with_friendly_message(self, db, user, account, monkeypatch):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", reference="r", source="akahu",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise _make_integrity_error(
                'duplicate key value violates unique constraint '
                '"ix_transactions_account_reference_dedupe"'
            )

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"reference": "r"})

        response = await tx_router.edit_transaction_modal(
            tx_id=existing.id, request=request, user=user, db=db,
        )

        assert response.status_code == 409
        body = response.body.decode()
        assert "already uses this reference" in body

    async def test_unrelated_integrity_error_is_not_swallowed(
        self, db, user, account, monkeypatch,
    ):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", reference=None, source="manual",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise _make_integrity_error("some other constraint blew up")

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"description": "new"})

        with pytest.raises(IntegrityError):
            await tx_router.edit_transaction_modal(
                tx_id=existing.id, request=request, user=user, db=db,
            )


@pytest.mark.asyncio
class TestFormRedirectsOnDedupeViolation:
    async def test_redirects_with_error_query_param(
        self, db, user, account, monkeypatch,
    ):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", reference="r", source="akahu",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise _make_integrity_error(
                'duplicate key value violates unique constraint '
                '"ix_transactions_account_reference_dedupe"'
            )

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))
        monkeypatch.setattr(
            tx_router.tx_svc, "get_transaction",
            AsyncMock(return_value=existing),
        )

        request = AsyncMock()
        response = await tx_router.update_transaction(
            tx_id=existing.id, request=request,
            account_id=str(account.id), tx_date="2026-04-18",
            amount="-1.00", description="x", category_id="",
            reference="r", notes="",
            user=user, db=db,
        )

        assert response.status_code == 302
        assert "error=duplicate_reference" in response.headers["location"]
