"""Tests for the single balance authority (:mod:`app.services.balances`).

The whole point of compartment #1 is that balances are derived on read from
the ledger, in exactly one place. These tests pin the derive-on-read
invariant, the basis filters, the inclusive ``as_of`` boundary, cross-screen
agreement, and the single-effect property of interest accrual.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.account import (
    Account,
    AccountTerm,
    AccountType,
    CompoundingFrequency,
    CompoundingType,
)
from app.models.transaction import Transaction
from app.services.balances import (
    BalanceBasis,
    aggregate_net_worth,
    balance,
    balances_for,
    net_worth,
)
from app.services.interest import accrue_interest_for_account


def _make_account(
    user_id,
    *,
    account_type=AccountType.CHECKING,
    initial_balance=Decimal("0.00"),
    is_active=True,
    name="Acct",
) -> Account:
    return Account(
        id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        account_type=account_type,
        currency="NZD",
        initial_balance=initial_balance,
        term=AccountTerm.SHORT,
        is_active=is_active,
    )


def _tx(
    account: Account,
    amount: Decimal,
    *,
    day: date = date(2026, 3, 1),
    is_pending: bool = False,
    is_cleared: bool = False,
) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        user_id=account.user_id,
        account_id=account.id,
        date=day,
        amount=amount,
        description="tx",
        original_description="tx",
        is_pending=is_pending,
        is_cleared=is_cleared,
    )


@pytest.mark.asyncio
class TestDeriveOnReadInvariant:
    async def test_all_basis_equals_initial_plus_sum(self, db, user):
        """balance(ALL) == initial_balance + Σ(amount) for a random ledger."""
        rng = random.Random(42)
        acct = _make_account(user.id, initial_balance=Decimal("123.45"))
        db.add(acct)
        await db.flush()

        total = Decimal("0.00")
        for _ in range(40):
            amt = Decimal(str(rng.randint(-50000, 50000))) / Decimal("100")
            db.add(_tx(acct, amt, is_pending=rng.random() < 0.3))
            total += amt
        await db.flush()

        assert await balance(db, acct.id, basis=BalanceBasis.ALL) == (
            Decimal("123.45") + total
        )

    async def test_posted_excludes_pending(self, db, user):
        acct = _make_account(user.id, initial_balance=Decimal("100.00"))
        db.add(acct)
        await db.flush()
        db.add(_tx(acct, Decimal("-30.00")))
        db.add(_tx(acct, Decimal("-5.00"), is_pending=True))
        await db.flush()

        assert await balance(db, acct.id, basis=BalanceBasis.POSTED) == Decimal("70.00")
        assert await balance(db, acct.id, basis=BalanceBasis.ALL) == Decimal("65.00")

    async def test_cleared_is_subset_of_posted(self, db, user):
        acct = _make_account(user.id, initial_balance=Decimal("0.00"))
        db.add(acct)
        await db.flush()
        db.add(_tx(acct, Decimal("200.00"), is_cleared=True))
        db.add(_tx(acct, Decimal("-50.00")))  # posted, not cleared
        await db.flush()

        assert await balance(db, acct.id, basis=BalanceBasis.CLEARED) == Decimal("200.00")
        assert await balance(db, acct.id, basis=BalanceBasis.POSTED) == Decimal("150.00")

    async def test_as_of_is_inclusive(self, db, user):
        acct = _make_account(user.id, initial_balance=Decimal("0.00"))
        db.add(acct)
        await db.flush()
        db.add(_tx(acct, Decimal("10.00"), day=date(2026, 3, 1)))
        db.add(_tx(acct, Decimal("20.00"), day=date(2026, 3, 2)))
        db.add(_tx(acct, Decimal("40.00"), day=date(2026, 3, 3)))
        await db.flush()

        # as_of includes transactions dated exactly on the boundary.
        assert await balance(db, acct.id, as_of=date(2026, 3, 2)) == Decimal("30.00")
        assert await balance(db, acct.id, as_of=date(2026, 3, 1)) == Decimal("10.00")
        # A day before the first tx → only the initial balance.
        assert await balance(db, acct.id, as_of=date(2026, 2, 28)) == Decimal("0.00")

    async def test_unknown_account_returns_zero(self, db, user):
        assert await balance(db, uuid.uuid4()) == Decimal("0.00")


@pytest.mark.asyncio
class TestBalancesFor:
    async def test_includes_zero_tx_accounts_at_initial(self, db, user):
        a = _make_account(user.id, initial_balance=Decimal("500.00"), name="A")
        b = _make_account(user.id, initial_balance=Decimal("0.00"), name="B")
        db.add_all([a, b])
        await db.flush()
        db.add(_tx(b, Decimal("75.00")))
        await db.flush()

        bals = await balances_for(db, user.id)
        assert bals[a.id] == Decimal("500.00")  # no txns → initial
        assert bals[b.id] == Decimal("75.00")

    async def test_scoped_to_user(self, db, user):
        other = type(user)(
            id=uuid.uuid4(),
            email="other@example.com",
            password_hash="x",
            display_name="Other",
        )
        db.add(other)
        await db.flush()
        mine = _make_account(user.id, initial_balance=Decimal("10.00"))
        theirs = _make_account(other.id, initial_balance=Decimal("999.00"))
        db.add_all([mine, theirs])
        await db.flush()

        bals = await balances_for(db, user.id)
        assert mine.id in bals
        assert theirs.id not in bals

    async def test_empty_account_ids_returns_empty(self, db, user):
        assert await balances_for(db, user.id, account_ids=[]) == {}


@pytest.mark.asyncio
class TestCrossScreenAgreement:
    async def test_net_worth_matches_manual_aggregate(self, db, user):
        """Every net-worth surface routes through the same two functions —
        ``balances_for`` + ``aggregate_net_worth`` — so they cannot diverge."""
        checking = _make_account(
            user.id, account_type=AccountType.CHECKING,
            initial_balance=Decimal("2000.00"), name="Checking",
        )
        savings = _make_account(
            user.id, account_type=AccountType.SAVINGS,
            initial_balance=Decimal("8000.00"), name="Savings",
        )
        card = _make_account(
            user.id, account_type=AccountType.CREDIT_CARD,
            initial_balance=Decimal("-1500.00"), name="Visa",
        )
        db.add_all([checking, savings, card])
        await db.flush()
        db.add(_tx(checking, Decimal("-200.00")))
        db.add(_tx(card, Decimal("-300.00")))
        await db.flush()

        accounts = [checking, savings, card]
        bals = await balances_for(db, user.id, account_ids=[a.id for a in accounts])
        manual = aggregate_net_worth(accounts, bals)

        canonical = await net_worth(db, user.id)

        # All active → canonical net_worth() equals the manual roll-up the
        # dashboard/accounts/reports screens each perform.
        assert canonical.assets == manual.assets
        assert canonical.liabilities == manual.liabilities
        assert canonical.net == manual.net
        # Sanity on the actual numbers.
        assert manual.assets == Decimal("9800.00")   # 1800 + 8000
        assert manual.liabilities == Decimal("1800.00")  # abs(-1800)
        assert manual.net == Decimal("8000.00")

    async def test_net_worth_excludes_inactive(self, db, user):
        active = _make_account(
            user.id, initial_balance=Decimal("1000.00"), name="Active",
        )
        inactive = _make_account(
            user.id, initial_balance=Decimal("5000.00"),
            is_active=False, name="Closed",
        )
        db.add_all([active, inactive])
        await db.flush()

        nw = await net_worth(db, user.id)
        assert nw.net == Decimal("1000.00")


@pytest.mark.asyncio
class TestInterestSingleEffect:
    async def test_accrual_moves_balance_by_exactly_the_tx(self, db, user):
        """Interest accrual's only side effect on the balance is the inserted
        ledger transaction — no hidden cache bump on top of it."""
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        acct = _make_account(
            user.id, account_type=AccountType.SAVINGS,
            initial_balance=Decimal("10000.00"), name="Savings",
        )
        acct.interest_rate = Decimal("5.0000")
        acct.compounding_type = CompoundingType.COMPOUND
        acct.compounding_frequency = CompoundingFrequency.MONTHLY
        acct.interest_last_accrued_at = anchor
        acct.opened_on = anchor.date()
        db.add(acct)
        await db.flush()

        before = await balance(db, acct.id, basis=BalanceBasis.POSTED)
        tx = await accrue_interest_for_account(
            db, acct, now=anchor + timedelta(days=30),
        )
        after = await balance(db, acct.id, basis=BalanceBasis.POSTED)

        assert tx is not None
        assert after - before == tx.amount
