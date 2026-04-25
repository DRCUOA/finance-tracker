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

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.account import (
    Account,
    AccountType,
    AccountTerm,
    CompoundingFrequency,
    CompoundingType,
)
from app.models.transaction import Transaction
from app.services.interest import (
    INTEREST_MARKER,
    INTEREST_SOURCE,
    InterestNotConfiguredError,
    RetroResult,
    RetroTxView,
    accrue_due_interest,
    accrue_interest_for_account,
    compute_interest,
    retro_reevaluate_interest,
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
        # Default to a recent date; tests that care about the retro window
        # override this explicitly. Always set it here so the column's NOT
        # NULL constraint is satisfied across the whole test suite.
        opened_on=date(2026, 1, 1),
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
        opened_on=date(2026, 1, 1),
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


@pytest.mark.asyncio
class TestSchedulerDescriptionMarker:
    """Every scheduled accrual must carry the shared interest marker so the
    retro re-evaluation can sum 'what's been posted'. This is the
    identification contract."""

    async def test_scheduled_accrual_description_contains_marker(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(user.id, interest_last_accrued_at=anchor)
        db.add(acct)
        await db.flush()

        tx = await accrue_interest_for_account(
            db, acct, now=anchor + timedelta(days=30),
        )
        assert tx is not None
        assert INTEREST_MARKER in tx.description
        assert INTEREST_MARKER in (tx.original_description or "")


# ---------------------------------------------------------------------------
# Retro re-evaluation
# ---------------------------------------------------------------------------


def _post_tx(account: Account, day: date, amount: Decimal, description: str) -> Transaction:
    """Build a non-interest transaction. Tests add it to the session."""
    return Transaction(
        id=uuid.uuid4(),
        user_id=account.user_id,
        account_id=account.id,
        date=day,
        amount=amount,
        description=description,
        original_description=description,
        is_pending=False,
    )


@pytest.mark.asyncio
class TestRetroReevaluate:
    async def test_raises_when_rate_unset(self, db, user):
        acct = _make_savings(user.id, interest_rate=None)
        db.add(acct)
        await db.flush()

        with pytest.raises(InterestNotConfiguredError):
            await retro_reevaluate_interest(db, acct)

    async def test_no_history_no_op(self, db, user):
        # created_at == now → no completed days → nothing to evaluate.
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(user.id)
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        result = await retro_reevaluate_interest(db, acct, now=anchor)
        # Always returns a populated RetroResult so the modal can render.
        assert result.new_transaction is None
        assert result.num_days == 0
        assert result.delta == Decimal("0.00")

    async def test_simple_full_year_no_prior_interest(self, db, user):
        """30 days of $10000 @ 5% APR simple = 1000 * 0.05 * 30/365 = 41.10
        but on $10000 = 10000 * 0.05 * 30/365 = 41.10*10 = 41.0959..."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert result.new_transaction is not None
        tx = result.new_transaction
        assert INTEREST_MARKER in tx.description
        # 10000 * 0.05 * 30 / 365 = 41.0958904...; rounds to 41.10
        # But our impl quantizes only the final accrued total, which sums
        # 30 daily accruals of 10000 * 0.05 / 365 = 1.36986... each. Sum =
        # 41.0958... → 41.10.
        assert tx.amount == Decimal("41.10")
        assert result.delta == Decimal("41.10")
        assert result.num_days == 30
        assert result.currency == "NZD"
        # Balance also bumped by the true-up amount.
        assert acct.current_balance == Decimal("10000.00") + tx.amount

    async def test_compound_annual_full_year_matches_textbook(self, db, user):
        """One year, compound annually @ 5% on $1000 → $50 of interest.
        With daily growth factor (1 + 0.05/1)^(1/365), compounding 365 times
        returns to (1.05)^1 = 1.05, so total interest = 50.00."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.COMPOUND,
            compounding_frequency=CompoundingFrequency.ANNUALLY,
            initial_balance=Decimal("1000.00"),
            current_balance=Decimal("1000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=365)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert result.new_transaction is not None
        # Tiny float drift across 365 daily compoundings is acceptable;
        # assert within a cent.
        assert abs(result.new_transaction.amount - Decimal("50.00")) <= Decimal("0.02")
        assert result.compounding_type_label == "Compound"
        assert result.compounding_frequency_label == "Annually"

    async def test_truup_reflects_existing_interest(self, db, user):
        """If half the expected interest has already been posted (with the
        marker), the true-up is the other half."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        # Post $20.00 of interest already (with the marker).
        existing = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            date=anchor.date() + timedelta(days=15),
            amount=Decimal("20.00"),
            description=f"Mid-period interest — {INTEREST_MARKER}",
            original_description="Mid-period interest",
            is_pending=False,
        )
        db.add(existing)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        # Expected total accrual ≈ 41.10; already posted 20.00; true-up ≈ 21.10
        assert result.new_transaction is not None
        assert result.new_transaction.amount == Decimal("21.10")
        assert result.delta == Decimal("21.10")
        # Existing interest tx is exposed in the result for the modal table.
        assert len(result.existing_transactions) == 1
        assert result.existing_transactions[0].amount == Decimal("20.00")
        assert result.actual_signed == Decimal("20.00")

    async def test_no_op_when_already_in_sync(self, db, user):
        """Run retro twice in a row — the second run sees the true-up from
        the first run and computes a delta of zero, posting nothing."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        first = await retro_reevaluate_interest(db, acct, now=now)
        assert first.new_transaction is not None

        second = await retro_reevaluate_interest(db, acct, now=now)
        assert second.new_transaction is None
        assert second.delta == Decimal("0.00")
        # Modal still has data: the second pass sees the first pass's tx
        # listed as an existing interest transaction.
        assert len(second.existing_transactions) >= 1

    async def test_clawback_when_overcharged(self, db, user):
        """If more interest has been posted than the simulation expects,
        the true-up is negative (a clawback for an asset)."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        # $200 of interest already posted — way more than 30d @ 5% deserves.
        over = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            date=anchor.date() + timedelta(days=10),
            amount=Decimal("200.00"),
            description=f"Bogus interest — {INTEREST_MARKER}",
            original_description="Bogus interest",
            is_pending=False,
        )
        db.add(over)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert result.new_transaction is not None
        # Expected ~41.10; actual 200.00; delta ~ -158.90
        assert result.new_transaction.amount < 0
        assert abs(result.new_transaction.amount - Decimal("-158.90")) <= Decimal("0.02")
        assert result.delta == result.new_transaction.amount

    async def test_credit_card_truup_is_negative(self, db, user):
        """Liability accounts have interest posted as negative (debt grows).
        A retro evaluation on a credit card with no prior interest produces
        a negative true-up."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_credit_card(user.id)
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        # initial == current means the simulation finds the balance at -2000
        # for the entire window.
        acct.initial_balance = acct.current_balance
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert result.new_transaction is not None
        amt = result.new_transaction.amount
        assert amt < 0
        # Sanity: 2000 @ 19.95% APR for 30d, daily compound:
        # daily growth ≈ 0.0001995/365... actually freq=DAILY so n=365,
        # rate_per_period = 0.1995/365, growth_per_day = (1+0.1995/365)^1 - 1
        # = 0.0005465... over 30 days ≈ 0.01646 → ~33 of interest.
        # Just sanity-check it's nontrivial.
        assert abs(amt) > Decimal("30.00")
        assert abs(amt) < Decimal("40.00")

    async def test_non_interest_transactions_change_simulation(self, db, user):
        """A mid-period deposit should grow the post-deposit balance,
        producing more total interest than a static balance would."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # Account A: starts at 10000, no activity.
        a = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        a.created_at = anchor
        a.opened_on = anchor.date()
        db.add(a)

        # Account B: starts at 10000, gets +5000 deposit on day 10.
        b = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("15000.00"),
        )
        b.created_at = anchor
        b.opened_on = anchor.date()
        db.add(b)
        await db.flush()

        deposit = _post_tx(
            b, anchor.date() + timedelta(days=10),
            Decimal("5000.00"), "Salary",
        )
        db.add(deposit)
        await db.flush()

        now = anchor + timedelta(days=30)
        result_a = await retro_reevaluate_interest(db, a, now=now)
        result_b = await retro_reevaluate_interest(db, b, now=now)

        assert result_a.new_transaction is not None
        assert result_b.new_transaction is not None
        assert result_b.new_transaction.amount > result_a.new_transaction.amount, (
            "Account with mid-period deposit should accrue more interest "
            "than the flat-balance account"
        )

    async def test_does_not_touch_interest_last_accrued_at(self, db, user):
        """Retro is a parallel operation — it must not poke the scheduler's
        bookkeeping timestamp, otherwise tomorrow's scheduled run could
        skip or double-count."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler_anchor = datetime(2026, 1, 25, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
            interest_last_accrued_at=scheduler_anchor,
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert result.new_transaction is not None
        # Untouched.
        assert acct.interest_last_accrued_at == scheduler_anchor


@pytest.mark.asyncio
class TestRetroResultShape:
    """The modal contracts on RetroResult — these tests pin the shape so
    template changes can't silently lose a field."""

    async def test_result_populates_modal_summary_fields(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
            interest_rate=Decimal("5.0000"),
            compounding_frequency=CompoundingFrequency.MONTHLY,
            currency="USD",
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert isinstance(result, RetroResult)
        assert result.rate == Decimal("5.0000")
        assert result.compounding_type_label == "Simple"
        assert result.compounding_frequency_label == "Monthly"
        assert result.start_date == anchor.date()
        assert result.end_date == now.date()
        assert result.num_days == 30
        assert result.initial_balance == Decimal("10000.00")
        assert result.currency == "USD"
        # Math fields are populated and self-consistent.
        assert result.delta == result.expected_signed - result.actual_signed
        # No prior interest, so actual is zero and delta == expected.
        assert result.actual_signed == Decimal("0.00")
        assert result.delta == result.expected_signed

    async def test_existing_transactions_are_plain_views(self, db, user):
        """Existing interest tx must be returned as RetroTxView (not ORM
        objects) so the template can render after the session closes."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        existing = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            date=anchor.date() + timedelta(days=10),
            amount=Decimal("5.00"),
            description=f"Prior accrual — {INTEREST_MARKER}",
            original_description="Prior accrual",
            is_pending=False,
        )
        db.add(existing)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert len(result.existing_transactions) == 1
        view = result.existing_transactions[0]
        assert isinstance(view, RetroTxView)
        assert view.amount == Decimal("5.00")
        assert INTEREST_MARKER in view.description

    async def test_new_transaction_view_matches_posted_amount(self, db, user):
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
        )
        acct.created_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        now = anchor + timedelta(days=30)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert isinstance(result.new_transaction, RetroTxView)
        # The view's amount mirrors the delta exactly.
        assert result.new_transaction.amount == result.delta
        assert result.new_transaction.date == now.date()

    async def test_backdating_opened_on_extends_retro_window(self, db, user):
        """Backdating ``opened_on`` to before ``created_at`` must make the
        retro walk further back. This is the whole point of the field: a
        long-standing real-world account added to the tracker mid-life
        should be able to replay its full history.
        """
        # Row inserted today, but the account was really opened a year ago.
        created = datetime(2026, 4, 23, tzinfo=timezone.utc)
        opened = date(2025, 4, 23)
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
            opened_on=opened,
        )
        acct.created_at = created
        db.add(acct)
        await db.flush()

        result = await retro_reevaluate_interest(db, acct, now=created)

        # Window spans the full year, not the zero-day gap between
        # created_at and now.
        assert result.start_date == opened
        assert result.end_date == created.date()
        assert result.num_days == 365
        # 10000 @ 5% simple for 365 days = 500.00
        assert result.new_transaction is not None
        assert abs(result.new_transaction.amount - Decimal("500.00")) <= Decimal("0.02")

    async def test_opened_on_after_created_at_shrinks_window(self, db, user):
        """Forward-dating ``opened_on`` (unusual, but the UI permits it)
        shortens the retro window accordingly — the service trusts the
        user-supplied open date over the audit timestamp."""
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        opened = date(2026, 4, 1)  # 3 months after created_at
        acct = _make_savings(
            user.id,
            compounding_type=CompoundingType.SIMPLE,
            initial_balance=Decimal("10000.00"),
            current_balance=Decimal("10000.00"),
            opened_on=opened,
        )
        acct.created_at = created
        db.add(acct)
        await db.flush()

        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        result = await retro_reevaluate_interest(db, acct, now=now)

        assert result.start_date == opened
        assert result.num_days == 30  # April has 30 days
