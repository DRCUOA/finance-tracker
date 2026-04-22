"""Tests for feed reconciliation: delta math, pending handling, staleness.

These cover the computation layer in ``app.services.feed_reconciliation``
and a handful of invariants on top of the Akahu sync (pending rows must
not affect transaction-derived balance; the posted sync must not tombstone
pending rows; etc.).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.account import Account, AccountTerm, AccountType
from app.models.transaction import Transaction
from app.services.accounts import recalculate_balance
from app.services.akahu import (
    AKAHU_SOURCE,
    sync_account_balances,
    sync_account_pending_transactions,
    sync_account_transactions,
)
from app.services.feed_reconciliation import (
    DELTA_NOISE_FLOOR,
    LAG_THRESHOLD_HOURS,
    STALE_THRESHOLD_HOURS,
    account_feed_status,
    user_feed_status,
)
from tests.conftest import make_akahu_account, make_akahu_transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mk_tx(
    db, user_id, account_id, amount, *, is_pending=False, tx_date=None, tx_id=None,
):
    tx = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=tx_date or date(2026, 4, 15),
        amount=Decimal(str(amount)),
        description="test tx",
        original_description="test tx",
        source=AKAHU_SOURCE,
        akahu_transaction_id=tx_id or f"tx_{uuid.uuid4().hex[:8]}",
        akahu_account_id="acc_test_123",
        is_pending=is_pending,
    )
    db.add(tx)
    await db.flush()
    return tx


# ---------------------------------------------------------------------------
# Pure computation — delta math & classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAccountFeedStatusMath:
    async def test_no_reported_balance_yields_none_delta(self, db, user, account):
        # Leave reported_balance unset — never synced.
        await _mk_tx(db, user.id, account.id, -50.00)
        status = await account_feed_status(db, account)
        assert status.reported_balance is None
        assert status.unreconciled_delta is None
        assert status.health == "no_delta_info"

    async def test_in_sync_when_reported_matches_posted(self, db, user, account):
        # initial 0 + sum(-50 + -30) = -80 posted. Reported says -80 too.
        await _mk_tx(db, user.id, account.id, -50)
        await _mk_tx(db, user.id, account.id, -30)
        account.reported_balance = Decimal("-80.00")
        account.reported_balance_as_of = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.flush()

        status = await account_feed_status(db, account)
        assert status.posted_balance == Decimal("-80.00")
        assert status.unreconciled_delta == Decimal("0.00")
        assert status.health == "in_sync"

    async def test_delta_equals_gap_between_reported_and_posted(self, db, user, account):
        # Posted totals -100, reported is -120 — bank sees $20 more spent
        # than our posted transactions can explain.
        await _mk_tx(db, user.id, account.id, -100)
        account.reported_balance = Decimal("-120.00")
        account.reported_balance_as_of = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.flush()

        status = await account_feed_status(db, account)
        assert status.posted_balance == Decimal("-100.00")
        assert status.unreconciled_delta == Decimal("-20.00")
        # Within lag threshold and has a non-zero delta: "lagging".
        assert status.health == "lagging"

    async def test_pending_absorbs_delta(self, db, user, account):
        # Posted -100, one pending -20, reported -120 -> delta 0
        await _mk_tx(db, user.id, account.id, -100)
        await _mk_tx(db, user.id, account.id, -20, is_pending=True)
        account.reported_balance = Decimal("-120.00")
        account.reported_balance_as_of = datetime.now(timezone.utc)
        await db.flush()

        status = await account_feed_status(db, account)
        assert status.posted_balance == Decimal("-100.00")
        assert status.pending_total == Decimal("-20.00")
        assert status.unreconciled_delta == Decimal("0.00")
        assert status.health == "in_sync"

    async def test_noise_floor_treated_as_in_sync(self, db, user, account):
        # Sub-cent rounding drift should not show as lagging.
        await _mk_tx(db, user.id, account.id, -100)
        account.reported_balance = Decimal("-100.00") + (DELTA_NOISE_FLOOR / 2)
        account.reported_balance_as_of = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.flush()

        status = await account_feed_status(db, account)
        assert abs(status.unreconciled_delta) < DELTA_NOISE_FLOOR
        assert status.health == "in_sync"

    async def test_stale_when_as_of_older_than_threshold(self, db, user, account):
        await _mk_tx(db, user.id, account.id, -100)
        account.reported_balance = Decimal("-100.00")
        account.reported_balance_as_of = datetime.now(timezone.utc) - timedelta(
            hours=STALE_THRESHOLD_HOURS + 1
        )
        await db.flush()

        status = await account_feed_status(db, account)
        # Delta is zero but the feed itself is stale — surface that.
        assert status.health == "stale"
        assert status.feed_lag_hours > STALE_THRESHOLD_HOURS

    async def test_lagging_when_as_of_between_thresholds(self, db, user, account):
        await _mk_tx(db, user.id, account.id, -100)
        account.reported_balance = Decimal("-100.00")
        account.reported_balance_as_of = datetime.now(timezone.utc) - timedelta(
            hours=LAG_THRESHOLD_HOURS + 1
        )
        await db.flush()

        status = await account_feed_status(db, account)
        assert status.health == "lagging"

    async def test_unlinked_account(self, db, user, unlinked_account):
        await _mk_tx(db, user.id, unlinked_account.id, -40)
        status = await account_feed_status(db, unlinked_account)
        assert status.is_linked is False
        assert status.reported_balance is None
        assert status.unreconciled_delta is None
        assert status.health == "unlinked"


@pytest.mark.asyncio
class TestUserFeedStatus:
    async def test_linked_only_filters_unlinked(self, db, user, account, unlinked_account):
        rows = await user_feed_status(db, user.id, linked_only=True)
        ids = {r.account_id for r in rows}
        assert account.id in ids
        assert unlinked_account.id not in ids

    async def test_returns_all_active_accounts_by_default(
        self, db, user, account, unlinked_account
    ):
        rows = await user_feed_status(db, user.id)
        ids = {r.account_id for r in rows}
        assert account.id in ids
        assert unlinked_account.id in ids


# ---------------------------------------------------------------------------
# Balance recalc — pending must not count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRecalculateBalanceExcludesPending:
    async def test_pending_does_not_affect_current_balance(self, db, user, account):
        await _mk_tx(db, user.id, account.id, -100)
        await _mk_tx(db, user.id, account.id, -30, is_pending=True)

        bal = await recalculate_balance(db, account.id)
        # Only the posted -100 counts; pending -30 stays in its own lane.
        assert bal == account.initial_balance + Decimal("-100.00")
        await db.refresh(account)
        assert account.current_balance == account.initial_balance + Decimal("-100.00")


# ---------------------------------------------------------------------------
# Balance sync — writes reported_balance, never current_balance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncAccountBalancesFeedFields:
    @patch("app.services.akahu.fetch_accounts")
    async def test_writes_reported_balance_not_current(self, mock_fetch, db, user, account):
        # Seed current_balance separately so we can verify it's untouched.
        account.current_balance = Decimal("999.99")
        await db.flush()

        mock_fetch.return_value = [
            make_akahu_account(akahu_id="acc_test_123", balance_current=2500.00)
        ]
        await sync_account_balances(db, user.id)
        await db.refresh(account)

        assert account.reported_balance == Decimal("2500.00")
        assert account.reported_balance_as_of is not None
        # current_balance is transaction-derived and should not be touched
        # by the balance sync.
        assert account.current_balance == Decimal("999.99")

    @patch("app.services.akahu.fetch_accounts")
    async def test_captures_refreshed_balance_timestamp(self, mock_fetch, db, user, account):
        akahu_acct = make_akahu_account(balance_current=100.00)
        akahu_acct["refreshed"] = {"balance": "2026-04-20T09:30:00.000Z"}
        mock_fetch.return_value = [akahu_acct]

        await sync_account_balances(db, user.id)
        await db.refresh(account)

        assert account.reported_balance_as_of == datetime(
            2026, 4, 20, 9, 30, 0, tzinfo=timezone.utc
        )


# ---------------------------------------------------------------------------
# Pending sync — insert, update, remove, and non-interference with posted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncAccountPendingTransactions:
    @patch("app.services.akahu.fetch_pending_transactions")
    async def test_inserts_pending_and_flags_it(self, mock_fetch, db, user, account):
        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="pending_001", amount=-15.00),
        ]
        result = await sync_account_pending_transactions(db, user.id)
        assert result["inserted"] == 1

        row = (await db.execute(
            select(Transaction).where(Transaction.akahu_transaction_id == "pending_001")
        )).scalar_one()
        assert row.is_pending is True

    @patch("app.services.akahu.fetch_pending_transactions")
    async def test_removes_pending_when_no_longer_in_feed(
        self, mock_fetch, db, user, account
    ):
        await _mk_tx(
            db, user.id, account.id, -22.50,
            is_pending=True, tx_id="pending_gone",
        )
        mock_fetch.return_value = []  # Akahu no longer reports it

        result = await sync_account_pending_transactions(db, user.id)
        assert result["removed"] == 1

        remaining = (await db.execute(
            select(Transaction).where(Transaction.akahu_transaction_id == "pending_gone")
        )).scalar_one_or_none()
        assert remaining is None

    @patch("app.services.akahu.fetch_pending_transactions")
    async def test_does_not_delete_posted_rows(self, mock_fetch, db, user, account):
        posted = await _mk_tx(
            db, user.id, account.id, -10.00,
            is_pending=False, tx_id="posted_safe",
        )
        mock_fetch.return_value = []  # nothing pending
        await sync_account_pending_transactions(db, user.id)

        still_there = (await db.execute(
            select(Transaction).where(Transaction.id == posted.id)
        )).scalar_one_or_none()
        assert still_there is not None

    @patch("app.services.akahu.fetch_account_transactions")
    async def test_posted_sync_does_not_mark_pending_stale(
        self, mock_fetch, db, user, account
    ):
        # A pending row dated in-range should be left alone by the posted
        # sync, even when it doesn't appear in the posted endpoint.
        pending = await _mk_tx(
            db, user.id, account.id, -5.00,
            is_pending=True, tx_id="still_pending",
            tx_date=date(2026, 4, 10),
        )
        mock_fetch.return_value = [
            make_akahu_transaction(tx_id="some_other_posted"),
        ]
        start_utc = "2026-04-09T12:00:00.000Z"
        end_utc = "2026-04-11T10:59:59.999Z"
        await sync_account_transactions(db, user.id, account.id, start_utc, end_utc)

        await db.refresh(pending)
        assert pending.is_source_stale is False
        assert pending.is_pending is True


# ---------------------------------------------------------------------------
# Freshness bookkeeping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTransactionsAsOf:
    @patch("app.services.akahu.fetch_account_transactions")
    async def test_posted_sync_updates_transactions_as_of(
        self, mock_fetch, db, user, account
    ):
        mock_fetch.return_value = [make_akahu_transaction()]
        assert account.transactions_as_of is None

        await sync_account_transactions(
            db, user.id, account.id,
            "2026-04-09T12:00:00.000Z", "2026-04-11T10:59:59.999Z",
        )
        await db.refresh(account)
        assert account.transactions_as_of is not None
