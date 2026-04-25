"""Tests for the printable-statement service.

Focus on the numbers, not the HTML. The template is a dumb render over the
dataclasses the service builds, so if we lock the pagination / running-balance
shape down here, any regression in the visible statement will show up as a
failing assertion (rather than a broken-looking PDF the user has to notice).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.account import Account, AccountTerm, AccountType
from app.models.transaction import Transaction
from app.models.user import User
from app.services import printable_statement as stmt


# ---------------------------------------------------------------------------
# Fixtures specific to these tests
# ---------------------------------------------------------------------------


async def _mk_txn(
    db,
    user: User,
    account: Account,
    dt: date,
    amount: str,
    description: str,
    reference: str | None = None,
    is_pending: bool = False,
):
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        date=dt,
        amount=Decimal(amount),
        description=description,
        reference=reference,
        is_pending=is_pending,
    )
    db.add(tx)
    await db.flush()
    return tx


@pytest_asyncio.fixture
async def account_with_history(db, user):
    """An account opened with a $100 initial balance and some prior activity.

    Prior activity (before the statement window) → exercises opening balance
    calculation, which is the trickiest bit of this service.
    """
    acct = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Everyday Cheque",
        account_type=AccountType.CHECKING,
        currency="NZD",
        initial_balance=Decimal("100.00"),
        current_balance=Decimal("100.00"),
        institution="ANZ",
        term=AccountTerm.SHORT,
    )
    db.add(acct)
    await db.flush()

    # Prior-period activity: +200 on 2026-01-05, -50 on 2026-01-20.
    # Opening balance for a statement starting 2026-02-01 should be:
    #   100 (initial) + 200 - 50 = 250.00
    await _mk_txn(db, user, acct, date(2026, 1, 5), "200.00", "Salary Jan")
    await _mk_txn(db, user, acct, date(2026, 1, 20), "-50.00", "Groceries Jan")
    return acct


# ---------------------------------------------------------------------------
# Opening balance
# ---------------------------------------------------------------------------


class TestOpeningBalance:
    @pytest.mark.asyncio
    async def test_opening_includes_all_txns_before_start(
        self, db, user, account_with_history,
    ):
        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        assert packet.accounts[0].opening_balance == Decimal("250.00")

    @pytest.mark.asyncio
    async def test_opening_equals_initial_when_no_prior_activity(self, db, user):
        acct = Account(
            id=uuid.uuid4(), user_id=user.id,
            name="Fresh Savings",
            account_type=AccountType.SAVINGS,
            currency="NZD",
            initial_balance=Decimal("500.00"),
            current_balance=Decimal("500.00"),
            term=AccountTerm.SHORT,
        )
        db.add(acct)
        await db.flush()

        packet = await stmt.build_statement(
            db, user, [acct.id],
            date(2026, 3, 1), date(2026, 3, 31),
        )
        assert packet.accounts[0].opening_balance == Decimal("500.00")

    @pytest.mark.asyncio
    async def test_opening_excludes_pending_transactions(
        self, db, user, account_with_history,
    ):
        # Add a pending -30 on 2026-01-25. It should NOT count toward opening
        # on 2026-02-01 because pending items haven't settled.
        await _mk_txn(
            db, user, account_with_history,
            date(2026, 1, 25), "-30.00", "Pending card auth",
            is_pending=True,
        )

        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        # Still 250 — pending txn ignored.
        assert packet.accounts[0].opening_balance == Decimal("250.00")


# ---------------------------------------------------------------------------
# Running balance & closing balance
# ---------------------------------------------------------------------------


class TestRunningBalance:
    @pytest.mark.asyncio
    async def test_running_balance_tracks_each_row(
        self, db, user, account_with_history,
    ):
        # Opening: 250.00
        # +400 on 2-05 → 650.00
        # -75 on 2-10 → 575.00
        # -25 on 2-12 → 550.00
        await _mk_txn(db, user, account_with_history, date(2026, 2, 5), "400.00", "Salary Feb")
        await _mk_txn(db, user, account_with_history, date(2026, 2, 10), "-75.00", "Power bill")
        await _mk_txn(db, user, account_with_history, date(2026, 2, 12), "-25.00", "Netflix")

        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        s = packet.accounts[0]

        assert s.opening_balance == Decimal("250.00")
        txns = [t for p in s.pages for t in p.transactions]
        assert [t.running_balance for t in txns] == [
            Decimal("650.00"),
            Decimal("575.00"),
            Decimal("550.00"),
        ]
        assert s.closing_balance == Decimal("550.00")
        assert s.total_credits == Decimal("400.00")
        assert s.total_debits == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_empty_period_yields_single_empty_page(
        self, db, user, account_with_history,
    ):
        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        s = packet.accounts[0]
        assert s.txn_count == 0
        assert len(s.pages) == 1
        assert s.pages[0].transactions == []
        # On an empty period closing == opening — critical for the summary row.
        assert s.closing_balance == s.opening_balance == Decimal("250.00")

    @pytest.mark.asyncio
    async def test_excludes_pending_from_running_and_closing(
        self, db, user, account_with_history,
    ):
        await _mk_txn(db, user, account_with_history, date(2026, 2, 5), "100.00", "Posted credit")
        await _mk_txn(
            db, user, account_with_history,
            date(2026, 2, 6), "-999.00", "Pending mega debit",
            is_pending=True,
        )
        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        s = packet.accounts[0]
        assert s.closing_balance == Decimal("350.00")  # 250 + 100
        assert s.txn_count == 1


# ---------------------------------------------------------------------------
# Pagination (brought-forward / carried-forward)
# ---------------------------------------------------------------------------


class TestPagination:
    @pytest.mark.asyncio
    async def test_multiple_pages_have_matching_carry_values(
        self, db, user, account_with_history,
    ):
        # 12 transactions, 5 per page → 3 pages: [5, 5, 2].
        for i in range(12):
            await _mk_txn(
                db, user, account_with_history,
                date(2026, 2, 1 + i),
                "10.00",
                f"Item {i}",
            )

        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
            rows_per_page=5,
        )
        s = packet.accounts[0]

        assert len(s.pages) == 3
        assert [len(p.transactions) for p in s.pages] == [5, 5, 2]

        # Page 0: brought-forward == opening; carried-forward == 5th row's
        # running balance = 250 + 5*10 = 300.
        assert s.pages[0].brought_forward == Decimal("250.00")
        assert s.pages[0].carried_forward == Decimal("300.00")

        # Page 1: brought-forward == previous page's carried = 300.
        # Carried-forward == 10th row's running = 250 + 10*10 = 350.
        assert s.pages[1].brought_forward == Decimal("300.00")
        assert s.pages[1].carried_forward == Decimal("350.00")

        # Page 2 (last): brought = 350; carried = 370 = closing.
        assert s.pages[2].brought_forward == Decimal("350.00")
        assert s.pages[2].carried_forward == Decimal("370.00")
        assert s.closing_balance == Decimal("370.00")

    @pytest.mark.asyncio
    async def test_rows_per_page_lower_bound_is_enforced(
        self, db, user, account_with_history,
    ):
        # Even if caller passes rows_per_page=1, the service clamps to 5 so
        # we never fragment into one-row pages.
        for i in range(3):
            await _mk_txn(
                db, user, account_with_history,
                date(2026, 2, 1 + i), "10.00", f"t{i}",
            )
        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
            rows_per_page=1,
        )
        # 3 rows @ 5 per page = 1 page.
        assert len(packet.accounts[0].pages) == 1


# ---------------------------------------------------------------------------
# Cross-account summary
# ---------------------------------------------------------------------------


class TestSummary:
    @pytest.mark.asyncio
    async def test_multiple_accounts_same_currency_roll_up(self, db, user):
        a1 = Account(
            id=uuid.uuid4(), user_id=user.id, name="A1",
            account_type=AccountType.CHECKING, currency="NZD",
            initial_balance=Decimal("100.00"), current_balance=Decimal("100.00"),
            term=AccountTerm.SHORT,
        )
        a2 = Account(
            id=uuid.uuid4(), user_id=user.id, name="A2",
            account_type=AccountType.SAVINGS, currency="NZD",
            initial_balance=Decimal("50.00"), current_balance=Decimal("50.00"),
            term=AccountTerm.SHORT,
        )
        db.add_all([a1, a2])
        await db.flush()

        await _mk_txn(db, user, a1, date(2026, 2, 5), "200.00", "credit")
        await _mk_txn(db, user, a2, date(2026, 2, 6), "-10.00", "debit")

        packet = await stmt.build_statement(
            db, user, [a1.id, a2.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )

        summary = packet.summary_by_currency["NZD"]
        assert summary["opening"] == Decimal("150.00")   # 100 + 50
        assert summary["credits"] == Decimal("200.00")
        assert summary["debits"] == Decimal("10.00")
        assert summary["closing"] == Decimal("340.00")    # 300 + 40
        assert summary["accounts"] == 2

    @pytest.mark.asyncio
    async def test_mixed_currencies_are_kept_separate(self, db, user):
        nzd_acct = Account(
            id=uuid.uuid4(), user_id=user.id, name="NZ",
            account_type=AccountType.CHECKING, currency="NZD",
            initial_balance=Decimal("100.00"), current_balance=Decimal("100.00"),
            term=AccountTerm.SHORT,
        )
        aud_acct = Account(
            id=uuid.uuid4(), user_id=user.id, name="AU",
            account_type=AccountType.CHECKING, currency="AUD",
            initial_balance=Decimal("200.00"), current_balance=Decimal("200.00"),
            term=AccountTerm.SHORT,
        )
        db.add_all([nzd_acct, aud_acct])
        await db.flush()

        packet = await stmt.build_statement(
            db, user, [nzd_acct.id, aud_acct.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )

        assert set(packet.summary_by_currency.keys()) == {"NZD", "AUD"}
        assert packet.summary_by_currency["NZD"]["opening"] == Decimal("100.00")
        assert packet.summary_by_currency["AUD"]["opening"] == Decimal("200.00")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestGuards:
    @pytest.mark.asyncio
    async def test_end_before_start_raises(self, db, user, account_with_history):
        with pytest.raises(ValueError):
            await stmt.build_statement(
                db, user, [account_with_history.id],
                date(2026, 3, 1), date(2026, 2, 1),
            )

    @pytest.mark.asyncio
    async def test_empty_account_list_raises(self, db, user):
        with pytest.raises(ValueError):
            await stmt.build_statement(
                db, user, [], date(2026, 2, 1), date(2026, 2, 28),
            )

    @pytest.mark.asyncio
    async def test_other_users_account_is_filtered_out(self, db, user):
        # Second user + their account; when we pass its ID it should be
        # silently dropped — the resulting packet has no accounts.
        other = User(
            id=uuid.uuid4(), email="other@example.com",
            password_hash="x", display_name="Other",
        )
        db.add(other)
        await db.flush()

        other_acct = Account(
            id=uuid.uuid4(), user_id=other.id, name="Other",
            account_type=AccountType.CHECKING, currency="NZD",
            initial_balance=Decimal("0"), current_balance=Decimal("0"),
            term=AccountTerm.SHORT,
        )
        db.add(other_acct)
        await db.flush()

        packet = await stmt.build_statement(
            db, user, [other_acct.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        assert packet.accounts == []


# ---------------------------------------------------------------------------
# Reference string
# ---------------------------------------------------------------------------


class TestReference:
    @pytest.mark.asyncio
    async def test_reference_encodes_period(
        self, db, user, account_with_history,
    ):
        packet = await stmt.build_statement(
            db, user, [account_with_history.id],
            date(2026, 2, 1), date(2026, 2, 28),
        )
        assert packet.reference.endswith("-260201-260228")
