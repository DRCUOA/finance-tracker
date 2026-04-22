"""Tests for interest accrual.

Two layers:

* Pure-math tests against :func:`compute_interest`. These are the canary —
  if they go red, the formula is wrong, not the wiring.
* Integration tests against :func:`accrue_interest_for_account` and
  :func:`accrue_due_interest` to lock in the side effects (transaction
  posted, balance updated, timestamp advanced, sign flipped for liabilities,
  no-op cases skipped).
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.account import (
    Account,
    AccountType,
    AccountTerm,
    CompoundingFrequency,
    CompoundingType,
)
from app.services.interest import (
    INTEREST_SOURCE,
    accrue_due_interest,
    accrue_interest_for_account,
    compute_interest,
)


# ---------------------------------------------------------------------------
# Pure-math tests
# ---------------------------------------------------------------------------


class TestComputeInterest:
    def test_zero_principal_returns_zero(self):
        assert compute_interest(
            Decimal("0"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.DAILY, 365,
        ) == Decimal("0.00")

    def test_zero_rate_returns_zero(self):
        assert compute_interest(
            Decimal("1000"), Decimal("0"),
            CompoundingType.COMPOUND, CompoundingFrequency.DAILY, 365,
        ) == Decimal("0.00")

    def test_zero_days_returns_zero(self):
        assert compute_interest(
            Decimal("1000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.DAILY, 0,
        ) == Decimal("0.00")

    def test_simple_interest_over_full_year(self):
        # I = P * r * t = 1000 * 0.05 * 1 = 50.00
        assert compute_interest(
            Decimal("1000"), Decimal("5"),
            CompoundingType.SIMPLE, CompoundingFrequency.MONTHLY, 365,
        ) == Decimal("50.00")

    def test_simple_interest_partial_year(self):
        # 90 days @ 5% APR on $1000 = 1000 * 0.05 * 90/365 ~ 12.33
        result = compute_interest(
            Decimal("1000"), Decimal("5"),
            CompoundingType.SIMPLE, CompoundingFrequency.MONTHLY, 90,
        )
        assert result == Decimal("12.33")

    def test_compound_annual_full_year_matches_simple(self):
        # Compounding annually for exactly one year is mathematically
        # identical to simple interest at the same rate: A = P(1+r)^1.
        result = compute_interest(
            Decimal("1000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.ANNUALLY, 365,
        )
        assert result == Decimal("50.00")

    def test_compound_monthly_full_year_beats_simple(self):
        # Monthly compounding @ 5% APR for a year:
        # A = 1000 * (1 + 0.05/12)^12 = 1051.16... so interest ~ 51.16
        result = compute_interest(
            Decimal("1000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.MONTHLY, 365,
        )
        assert result == Decimal("51.16")

    def test_compound_daily_highest_yield(self):
        # Daily compounding should beat monthly which beats annual.
        annual = compute_interest(
            Decimal("10000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.ANNUALLY, 365,
        )
        monthly = compute_interest(
            Decimal("10000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.MONTHLY, 365,
        )
        daily = compute_interest(
            Decimal("10000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.DAILY, 365,
        )
        assert annual < monthly < daily

    def test_compound_quarterly_textbook(self):
        # 10000 @ 8% APR compounded quarterly for 1 year:
        # A = 10000 * (1 + 0.02)^4 = 10824.32 so interest = 824.32
        result = compute_interest(
            Decimal("10000"), Decimal("8"),
            CompoundingType.COMPOUND, CompoundingFrequency.QUARTERLY, 365,
        )
        assert result == Decimal("824.32")

    def test_negative_principal_returns_zero(self):
        # Caller is expected to pass abs(balance); guard against misuse.
        assert compute_interest(
            Decimal("-1000"), Decimal("5"),
            CompoundingType.COMPOUND, CompoundingFrequency.DAILY, 365,
        ) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Integration tests against the DB
# ---------------------------------------------------------------------------


def _make_savings(user_id, **overrides) -> Account:
    base = dict(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Savings",
        account_type=AccountType.SAVINGS,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        current_balance=Decimal("10000.00"),
        institution="ANZ",
        term=AccountTerm.MEDIUM,
        interest_rate=Decimal("5.0000"),
        compounding_type=CompoundingType.COMPOUND,
        compounding_frequency=CompoundingFrequency.MONTHLY,
        is_active=True,
        is_cashflow=True,
    )
    base.update(overrides)
    return Account(**base)


def _make_credit_card(user_id, **overrides) -> Account:
    base = dict(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Visa",
        account_type=AccountType.CREDIT_CARD,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        current_balance=Decimal("-2000.00"),
        institution="Westpac",
        term=AccountTerm.SHORT,
        interest_rate=Decimal("19.9500"),
        compounding_type=CompoundingType.COMPOUND,
        compounding_frequency=CompoundingFrequency.DAILY,
        is_active=True,
        is_cashflow=True,
    )
    base.update(overrides)
    return Account(**base)


@pytest.mark.asyncio
class TestAccrueInterestForAccount:
    async def test_posts_positive_transaction_for_asset(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(user.id, interest_last_accrued_at=anchor)
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        tx = await accrue_interest_for_account(db, acct, now=now)

        assert tx is not None
        assert tx.source == INTEREST_SOURCE
        assert tx.amount > 0  # asset balance grows
        assert tx.account_id == acct.id
        assert tx.date == now.date()
        assert acct.interest_last_accrued_at == now
        # Balance must be updated in lockstep with the posted transaction.
        assert acct.current_balance == Decimal("10000.00") + tx.amount

    async def test_posts_negative_transaction_for_credit_card(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_credit_card(user.id, interest_last_accrued_at=anchor)
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        tx = await accrue_interest_for_account(db, acct, now=now)

        assert tx is not None
        assert tx.amount < 0  # debt grows = balance more negative
        assert acct.current_balance < Decimal("-2000.00")

    async def test_no_op_when_rate_unset(self, db, user):
        acct = _make_savings(user.id, interest_rate=None)
        db.add(acct)
        await db.flush()

        tx = await accrue_interest_for_account(db, acct)
        assert tx is None

    async def test_no_op_when_balance_zero(self, db, user):
        acct = _make_savings(user.id, current_balance=Decimal("0.00"))
        db.add(acct)
        await db.flush()

        tx = await accrue_interest_for_account(db, acct)
        assert tx is None

    async def test_no_op_when_no_days_elapsed(self, db, user):
        anchor = datetime(2026, 4, 1, tzinfo=timezone.utc)
        acct = _make_savings(user.id, interest_last_accrued_at=anchor)
        db.add(acct)
        await db.flush()

        # Same instant — zero elapsed days, nothing to accrue.
        tx = await accrue_interest_for_account(db, acct, now=anchor)
        assert tx is None

    async def test_first_run_seeds_timestamp_without_posting(self, db, user):
        # No prior accrual AND no created_at populated yet (server_default
        # only fires on commit, not on the in-memory object). The service
        # should plant a baseline so the *next* run has a delta.
        anchor = datetime(2026, 4, 1, tzinfo=timezone.utc)
        acct = _make_savings(user.id, interest_last_accrued_at=None)
        # Force created_at to None to simulate the unflushed-default state.
        acct.created_at = None
        db.add(acct)

        tx = await accrue_interest_for_account(db, acct, now=anchor)
        assert tx is None
        assert acct.interest_last_accrued_at == anchor

    async def test_advances_timestamp_when_interest_rounds_to_zero(self, db, user):
        # $0.01 at 1% APR over 1 day yields ~0.0000003, which rounds to 0.
        # The timestamp must still advance — otherwise tiny balances would
        # stay stuck and never accrue.
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            current_balance=Decimal("0.01"),
            interest_rate=Decimal("1.0000"),
            interest_last_accrued_at=anchor,
        )
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=1)
        tx = await accrue_interest_for_account(db, acct, now=now)
        assert tx is None
        assert acct.interest_last_accrued_at == now


@pytest.mark.asyncio
class TestAccrueDueInterest:
    async def test_skips_inactive_accounts(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        active = _make_savings(user.id, interest_last_accrued_at=anchor)
        inactive = _make_savings(
            user.id, is_active=False, interest_last_accrued_at=anchor,
        )
        db.add_all([active, inactive])
        await db.flush()

        now = anchor + timedelta(days=30)
        posted = await accrue_due_interest(db, now=now)

        assert len(posted) == 1
        assert posted[0].account_id == active.id

    async def test_skips_accounts_without_rate(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with_rate = _make_savings(user.id, interest_last_accrued_at=anchor)
        without = _make_savings(
            user.id, interest_rate=None, interest_last_accrued_at=anchor,
        )
        db.add_all([with_rate, without])
        await db.flush()

        now = anchor + timedelta(days=30)
        posted = await accrue_due_interest(db, now=now)

        assert len(posted) == 1
        assert posted[0].account_id == with_rate.id

    async def test_idempotent_within_same_day(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(user.id, interest_last_accrued_at=anchor)
        db.add(acct)
        await db.flush()

        # First run: 30 days elapsed, posts a transaction.
        now = anchor + timedelta(days=30)
        first = await accrue_due_interest(db, now=now)
        assert len(first) == 1

        # Second run at the same moment: timestamp already advanced to `now`,
        # so elapsed_days is 0 and nothing is posted.
        second = await accrue_due_interest(db, now=now)
        assert second == []
