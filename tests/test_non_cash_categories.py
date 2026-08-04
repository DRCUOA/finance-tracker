"""Tests for the non_cash category type.

Non-cash categories hold pure asset/liability value movements (interest
accruals, revaluations). The contract under test:

* the interest job files every accrual under the user's non-cash Interest
  category (creating "Non-Cash" > "Interest" on demand);
* cash-basis reports — period summary, spending pulse, income-vs-spending
  trend — exclude non-cash-categorised transactions entirely;
* the keyword categoriser never auto-suggests a non-cash (or transfer)
  category.
"""
from __future__ import annotations

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
from app.models.category import Category, CategoryKeyword, CategoryType
from app.models.transaction import Transaction
from app.services import reports as report_svc
from app.services.categories import (
    NON_CASH_INTEREST_NAME,
    NON_CASH_ROOT_NAME,
    get_or_create_interest_category,
    seed_default_categories,
)
from app.services.categoriser import suggest_category
from app.services.interest import accrue_interest_for_account


def _make_loan(user_id, **overrides) -> Account:
    base = dict(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Home Loan",
        account_type=AccountType.LOAN,
        currency="NZD",
        initial_balance=Decimal("-100000.00"),
        institution="ANZ",
        term=AccountTerm.LONG,
        interest_rate=Decimal("6.0000"),
        compounding_type=CompoundingType.COMPOUND,
        compounding_frequency=CompoundingFrequency.MONTHLY,
        is_active=True,
        is_cashflow=True,
        opened_on=date(2026, 1, 1),
    )
    base.update(overrides)
    return Account(**base)


async def _add_category(db, user_id, name, cat_type, parent_id=None) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, name=name,
        category_type=cat_type, parent_id=parent_id,
    )
    db.add(cat)
    await db.flush()
    return cat


def _tx(user_id, account_id, category_id, amount, on, description="tx") -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        account_id=account_id,
        category_id=category_id,
        date=on,
        amount=Decimal(str(amount)),
        description=description,
        is_cleared=True,
    )


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------


class TestGetOrCreateInterestCategory:
    @pytest.mark.asyncio
    async def test_creates_root_and_child_when_missing(self, db, user):
        cat = await get_or_create_interest_category(db, user.id)

        assert cat.name == NON_CASH_INTEREST_NAME
        assert cat.category_type == CategoryType.NON_CASH
        assert cat.parent_id is not None
        root = await db.get(Category, cat.parent_id)
        assert root.name == NON_CASH_ROOT_NAME
        assert root.category_type == CategoryType.NON_CASH
        assert root.parent_id is None

    @pytest.mark.asyncio
    async def test_idempotent(self, db, user):
        first = await get_or_create_interest_category(db, user.id)
        second = await get_or_create_interest_category(db, user.id)
        assert first.id == second.id

    @pytest.mark.asyncio
    async def test_reuses_seeded_default(self, db, user):
        await seed_default_categories(db, user.id)
        cat = await get_or_create_interest_category(db, user.id)

        # Seeding created the Non-Cash tree; the helper must not duplicate it.
        non_cash_cats = (await db.execute(
            Category.__table__.select().where(
                Category.user_id == user.id,
                Category.category_type == CategoryType.NON_CASH,
            )
        )).fetchall()
        assert len(non_cash_cats) == 2
        assert cat.category_type == CategoryType.NON_CASH

    @pytest.mark.asyncio
    async def test_does_not_confuse_income_interest_category(self, db, user):
        await seed_default_categories(db, user.id)
        cat = await get_or_create_interest_category(db, user.id)
        # The default tree also has "Income > Interest" — must not be picked.
        assert cat.category_type == CategoryType.NON_CASH


# ---------------------------------------------------------------------------
# Interest job files accruals as non-cash
# ---------------------------------------------------------------------------


class TestInterestAccrualCategorised:
    @pytest.mark.asyncio
    async def test_accrual_posts_into_non_cash_category(self, db, user):
        acct = _make_loan(
            user.id,
            interest_last_accrued_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        db.add(acct)
        await db.flush()

        tx = await accrue_interest_for_account(db, acct)

        assert tx is not None
        assert tx.category_id is not None
        cat = await db.get(Category, tx.category_id)
        assert cat.category_type == CategoryType.NON_CASH
        assert cat.name == NON_CASH_INTEREST_NAME


# ---------------------------------------------------------------------------
# Report exclusion
# ---------------------------------------------------------------------------


class TestReportsExcludeNonCash:
    @pytest.mark.asyncio
    async def test_period_summary_excludes_non_cash(self, db, user, unlinked_account):
        expense = await _add_category(db, user.id, "Groceries", CategoryType.EXPENSE)
        non_cash = await _add_category(db, user.id, "Interest", CategoryType.NON_CASH)

        on = date(2026, 7, 10)
        db.add(_tx(user.id, unlinked_account.id, expense.id, "-100.00", on))
        db.add(_tx(user.id, unlinked_account.id, non_cash.id, "-500.00", on,
                   description="Interest accrual"))
        await db.flush()

        summary = await report_svc.period_summary(
            db, user.id, date(2026, 7, 1), date(2026, 8, 1),
        )

        assert summary["expenses"] == pytest.approx(100.0)
        assert summary["income"] == pytest.approx(0.0)
        # The category itself still appears in the breakdown, typed non_cash,
        # so accrual-basis consumers can opt in.
        by_name = {c["name"]: c for c in summary["categories"]}
        assert by_name["Interest"]["type"] == CategoryType.NON_CASH

    @pytest.mark.asyncio
    async def test_spending_pulse_excludes_non_cash(self, db, user, unlinked_account):
        expense = await _add_category(db, user.id, "Groceries", CategoryType.EXPENSE)
        non_cash = await _add_category(db, user.id, "Interest", CategoryType.NON_CASH)

        today = date.today()
        start, end = report_svc.month_bounds(today.year, today.month)
        db.add(_tx(user.id, unlinked_account.id, expense.id, "-80.00", today))
        db.add(_tx(user.id, unlinked_account.id, non_cash.id, "-450.00", today,
                   description="Interest accrual"))
        await db.flush()

        pulse = await report_svc.weekly_spending_pulse(
            db, user.id, start, end, period="month",
        )

        assert pulse["total_actual"] == pytest.approx(80.0)
        day_totals = sum(d["amount"] for d in pulse["daily_spending"])
        assert day_totals == pytest.approx(80.0)
        cat_names = {c["name"] for d in pulse["daily_spending"] for c in d["categories"]}
        assert "Interest" not in cat_names

    @pytest.mark.asyncio
    async def test_income_vs_spending_trend_excludes_non_cash(
        self, db, user, unlinked_account,
    ):
        expense = await _add_category(db, user.id, "Groceries", CategoryType.EXPENSE)
        income = await _add_category(db, user.id, "Salary", CategoryType.INCOME)
        non_cash = await _add_category(db, user.id, "Interest", CategoryType.NON_CASH)

        today = date.today()
        db.add(_tx(user.id, unlinked_account.id, income.id, "1000.00", today))
        db.add(_tx(user.id, unlinked_account.id, expense.id, "-200.00", today))
        db.add(_tx(user.id, unlinked_account.id, non_cash.id, "-999.00", today))
        await db.flush()

        trend = await report_svc.income_vs_spending_trend(db, user.id, periods=1)

        assert trend["total_income"] == pytest.approx(1000.0)
        assert trend["total_expenses"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Categoriser never auto-suggests non-cash
# ---------------------------------------------------------------------------


class TestCategoriserExcludesNonCash:
    @pytest.mark.asyncio
    async def test_suggest_ignores_non_cash_keywords(self, db, user):
        non_cash = await _add_category(db, user.id, "Interest", CategoryType.NON_CASH)
        db.add(CategoryKeyword(id=uuid.uuid4(), category_id=non_cash.id, keyword="interest"))
        await db.flush()

        assert await suggest_category(db, user.id, "loan interest charged") is None

    @pytest.mark.asyncio
    async def test_suggest_still_matches_expense_keywords(self, db, user):
        expense = await _add_category(db, user.id, "Groceries", CategoryType.EXPENSE)
        db.add(CategoryKeyword(id=uuid.uuid4(), category_id=expense.id, keyword="supermarket"))
        await db.flush()

        assert await suggest_category(db, user.id, "countdown supermarket akl") == expense.id
