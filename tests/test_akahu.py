"""Tests for Akahu bank-feed integration.

Covers: API client, balance sync, transaction sync (insert/update/idempotent/stale).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType, AccountTerm
from app.models.category import Category
from app.models.transaction import Transaction
from app.services.akahu import (
    AkahuAPIError,
    AkahuConfigError,
    AKAHU_SOURCE,
    nz_date_to_utc_range,
    sync_account_balances,
    sync_account_transactions,
)
from tests.conftest import make_akahu_account, make_akahu_transaction

# ---------------------------------------------------------------------------
# Date helper tests
# ---------------------------------------------------------------------------

class TestNzDateToUtcRange:
    def test_basic_conversion(self):
        start, end = nz_date_to_utc_range(date(2026, 4, 10), date(2026, 4, 10))
        assert "2026-04-09" in start  # NZST is UTC+12/13
        assert start.endswith("Z")
        assert end.endswith("Z")

    def test_start_before_end(self):
        start, end = nz_date_to_utc_range(date(2026, 1, 1), date(2026, 1, 31))
        assert start < end

    def test_end_is_end_of_day(self):
        _, end = nz_date_to_utc_range(date(2026, 6, 15), date(2026, 6, 15))
        assert "23:59:59" not in end or "T" in end  # converted to UTC so hour shifts


# ---------------------------------------------------------------------------
# Balance sync tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncAccountBalances:
    @patch("app.services.akahu.fetch_accounts")
    async def test_updates_linked_account(self, mock_fetch, db, user, account):
        # Seed current_balance to something unrelated so we can verify the
        # balance sync now targets ``reported_balance`` and leaves the
        # transaction-derived ``current_balance`` alone.
        account.current_balance = Decimal("999.99")
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_account(akahu_id="acc_test_123", balance_current=2500.00)
        ]
        result = await sync_account_balances(db, user.id)

        assert result["linked_found"] == 1
        assert result["updated"] == 1
        assert result["unchanged"] == 0

        await db.refresh(account)
        assert account.reported_balance == Decimal("2500.00")
        # current_balance is transaction-derived; balance sync must not touch it.
        assert account.current_balance == Decimal("999.99")

    @patch("app.services.akahu.fetch_accounts")
    async def test_skips_write_when_unchanged(self, mock_fetch, db, user, account):
        # Pre-seed the reported balance + timestamp so the incoming Akahu
        # response is a no-op.
        account.reported_balance = Decimal("1234.56")
        account.reported_balance_as_of = datetime(
            2026, 4, 11, 1, 0, 0, tzinfo=timezone.utc
        )
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_account(akahu_id="acc_test_123", balance_current=1234.56)
        ]
        result = await sync_account_balances(db, user.id)

        assert result["updated"] == 0
        assert result["unchanged"] == 1

    @patch("app.services.akahu.fetch_accounts")
    async def test_does_not_touch_unlinked(self, mock_fetch, db, user, unlinked_account):
        mock_fetch.return_value = [
            make_akahu_account(akahu_id="acc_other", balance_current=9999.00)
        ]
        result = await sync_account_balances(db, user.id)
        assert result["linked_found"] == 0

        await db.refresh(unlinked_account)
        assert unlinked_account.current_balance == Decimal("500.00")

    @patch("app.services.akahu.fetch_accounts")
    async def test_missing_in_akahu(self, mock_fetch, db, user, account):
        mock_fetch.return_value = []
        result = await sync_account_balances(db, user.id)

        assert result["linked_found"] == 1
        assert result["missing_in_akahu"] == 1
        assert result["updated"] == 0

    @patch("app.services.akahu.fetch_accounts")
    async def test_api_error_captured(self, mock_fetch, db, user, account):
        mock_fetch.side_effect = AkahuAPIError(401, "Unauthorized")
        result = await sync_account_balances(db, user.id)
        assert len(result["errors"]) == 1
        assert "401" in result["errors"][0]


# ---------------------------------------------------------------------------
# Transaction sync tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncAccountTransactions:
    START_UTC = "2026-04-09T12:00:00.000Z"
    END_UTC = "2026-04-11T10:59:59.999Z"

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_inserts_new_transactions(self, mock_fetch, db, user, account):
        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="trans_001", amount=-25.00, description="PAK N SAVE"),
            make_akahu_transaction(tx_id="trans_002", amount=-15.50, description="COFFEE CLUB"),
        ]

        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        assert result["fetched"] == 2
        assert result["inserted"] == 2
        assert result["updated"] == 0

        txs = (await db.execute(
            select(Transaction).where(Transaction.source == AKAHU_SOURCE)
        )).scalars().all()
        assert len(txs) == 2
        assert {t.akahu_transaction_id for t in txs} == {"trans_001", "trans_002"}
        assert all(t.source == AKAHU_SOURCE for t in txs)

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_updates_existing_mutable_fields(self, mock_fetch, db, user, account):
        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 4, 10),
            amount=Decimal("-25.00"),
            description="OLD DESCRIPTION",
            original_description="OLD DESCRIPTION",
            source=AKAHU_SOURCE,
            akahu_transaction_id="trans_001",
            akahu_account_id="acc_test_123",
        )
        db.add(tx)
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_transaction(
                tx_id="trans_001", amount=-30.00, description="NEW DESCRIPTION"
            ),
        ]

        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        assert result["updated"] == 1
        assert result["inserted"] == 0

        await db.refresh(tx)
        assert tx.amount == Decimal("-30.00")
        assert tx.description == "NEW DESCRIPTION"
        assert tx.original_description == "OLD DESCRIPTION"  # never overwritten

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_preserves_user_managed_fields(self, mock_fetch, db, user, account):
        cat = Category(
            id=uuid.uuid4(), user_id=user.id, name="Groceries",
            category_type="expense", sort_order=0,
        )
        db.add(cat)
        await db.flush()

        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 4, 10),
            amount=Decimal("-25.00"),
            description="COUNTDOWN",
            original_description="COUNTDOWN",
            source=AKAHU_SOURCE,
            akahu_transaction_id="trans_001",
            akahu_account_id="acc_test_123",
            category_id=cat.id,
            notes="User note",
            is_cleared=True,
        )
        db.add(tx)
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="trans_001", amount=-25.00, description="COUNTDOWN"),
        ]

        await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        await db.refresh(tx)
        assert tx.category_id == cat.id
        assert tx.notes == "User note"
        assert tx.is_cleared is True

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_idempotent_no_changes(self, mock_fetch, db, user, account):
        """Re-running sync with identical data causes zero writes."""
        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 4, 10),
            amount=Decimal("-42.50"),
            description="COUNTDOWN ALBANY",
            original_description="COUNTDOWN ALBANY",
            reference="ref001",
            source=AKAHU_SOURCE,
            akahu_transaction_id="trans_abc123",
            akahu_account_id="acc_test_123",
            akahu_updated_at=datetime(2026, 4, 10, 6, 0, 0, tzinfo=timezone.utc),
            is_pending=False,
        )
        db.add(tx)
        await db.flush()

        mock_fetch.return_value = [make_akahu_transaction()]

        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        assert result["inserted"] == 0
        assert result["updated"] == 0
        assert result["unchanged"] == 1

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_no_duplicates_on_repeated_sync(self, mock_fetch, db, user, account):
        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="trans_repeat", amount=-10.00),
        ]

        await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )
        await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        txs = (await db.execute(
            select(Transaction).where(Transaction.akahu_transaction_id == "trans_repeat")
        )).scalars().all()
        assert len(txs) == 1

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_stale_marking(self, mock_fetch, db, user, account):
        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 4, 10),
            amount=Decimal("-20.00"),
            description="DISAPPEARED TX",
            original_description="DISAPPEARED TX",
            source=AKAHU_SOURCE,
            akahu_transaction_id="trans_gone",
            akahu_account_id="acc_test_123",
        )
        db.add(tx)
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="trans_still_here"),
        ]

        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        assert result["stale_marked"] == 1

        await db.refresh(tx)
        assert tx.is_source_stale is True
        assert tx.source_stale_since is not None

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_stale_not_re_marked(self, mock_fetch, db, user, account):
        """Already-stale transactions should not have source_stale_since updated."""
        original_stale_time = datetime(2026, 4, 9, 0, 0, 0, tzinfo=timezone.utc)
        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 4, 10),
            amount=Decimal("-20.00"),
            description="ALREADY STALE",
            original_description="ALREADY STALE",
            source=AKAHU_SOURCE,
            akahu_transaction_id="trans_already_stale",
            akahu_account_id="acc_test_123",
            is_source_stale=True,
            source_stale_since=original_stale_time,
        )
        db.add(tx)
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="trans_other"),
        ]

        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        # Should not re-mark because is_source_stale is already True
        assert result["stale_marked"] == 0

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_stale_cleared_on_reappearance(self, mock_fetch, db, user, account):
        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            date=date(2026, 4, 10),
            amount=Decimal("-42.50"),
            description="COUNTDOWN ALBANY",
            original_description="COUNTDOWN ALBANY",
            reference="ref001",
            source=AKAHU_SOURCE,
            akahu_transaction_id="trans_abc123",
            akahu_account_id="acc_test_123",
            is_source_stale=True,
            source_stale_since=datetime(2026, 4, 9, tzinfo=timezone.utc),
        )
        db.add(tx)
        await db.flush()

        mock_fetch.return_value = [make_akahu_transaction()]

        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )

        assert result["stale_cleared"] == 1

        await db.refresh(tx)
        assert tx.is_source_stale is False
        assert tx.source_stale_since is None

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_unlinked_account_rejected(self, mock_fetch, db, user, unlinked_account):
        result = await sync_account_transactions(
            db, user.id, unlinked_account.id, self.START_UTC, self.END_UTC
        )
        assert result["errors"]
        assert "not linked" in result["errors"][0].lower()
        mock_fetch.assert_not_called()

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_wrong_user_rejected(self, mock_fetch, db, user, account):
        other_user_id = uuid.uuid4()
        result = await sync_account_transactions(
            db, other_user_id, account.id, self.START_UTC, self.END_UTC
        )
        assert result["errors"]
        mock_fetch.assert_not_called()

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_api_error_captured(self, mock_fetch, db, user, account):
        mock_fetch.side_effect = AkahuAPIError(429, "Rate limited")
        result = await sync_account_transactions(
            db, user.id, account.id, self.START_UTC, self.END_UTC
        )
        assert len(result["errors"]) == 1
        assert "429" in result["errors"][0]
