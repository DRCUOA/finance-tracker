import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, func as sa_func, extract, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, ACCOUNT_TYPE_GROUPS, AccountGroup
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def week_bounds(ref: date) -> tuple[date, date]:
    """Return (monday, next_monday) for the ISO week containing *ref*."""
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=7)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def step_period(ref: date, direction: int, period: str) -> date:
    """Move *ref* forward (+1) or backward (-1) by one period unit."""
    if period == "week":
        return ref + timedelta(weeks=direction)
    m = ref.month + direction
    y = ref.year
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return date(y, m, min(ref.day, 28))


def period_bounds(ref: date, period: str) -> tuple[date, date]:
    if period == "week":
        return week_bounds(ref)
    return month_bounds(ref.year, ref.month)


def period_label(ref: date, period: str) -> str:
    if period == "week":
        mon, sun = week_bounds(ref)
        return f"Week of {mon.strftime('%b %d')} – {(sun - timedelta(days=1)).strftime('%b %d, %Y')}"
    return ref.strftime("%B %Y")


# ---------------------------------------------------------------------------
# Core report queries (now accept start/end date ranges)
# ---------------------------------------------------------------------------

async def period_summary(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
) -> dict:
    """Income vs expenses for an arbitrary date range, broken down by category."""
    stmt = (
        select(
            Category.id,
            Category.name,
            Category.category_type,
            Category.parent_id,
            sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")).label("total"),
        )
        .outerjoin(Transaction, and_(
            Transaction.category_id == Category.id,
            Transaction.date >= start,
            Transaction.date < end,
        ))
        .where(Category.user_id == user_id)
        .group_by(Category.id)
        .order_by(Category.sort_order)
    )
    result = await db.execute(stmt)
    rows = result.all()

    income = Decimal("0.00")
    expenses = Decimal("0.00")
    categories = []
    for row in rows:
        total = row.total or Decimal("0.00")
        categories.append({
            "id": str(row.id),
            "name": row.name,
            "type": row.category_type,
            "parent_id": str(row.parent_id) if row.parent_id else None,
            "total": float(total),
        })
        if row.category_type == CategoryType.INCOME:
            income += total
        elif row.category_type == CategoryType.EXPENSE:
            expenses += abs(total)

    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "income": float(income), "expenses": float(expenses),
        "net": float(income - expenses),
        "categories": categories,
    }


async def monthly_summary(
    db: AsyncSession, user_id: uuid.UUID, year: int, month: int,
) -> dict:
    start, end = month_bounds(year, month)
    result = await period_summary(db, user_id, start, end)
    result["year"] = year
    result["month"] = month
    return result


async def budget_vs_actual(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
    budget_year: int | None = None,
    budget_month: int | None = None,
    period: str = "month",
) -> list[dict]:
    by = budget_year or start.year
    bm = budget_month or start.month

    budget_stmt = (
        select(Budget.category_id, Budget.amount)
        .where(Budget.user_id == user_id, Budget.year == by, Budget.month == bm)
    )
    budget_result = await db.execute(budget_stmt)
    budgets = {str(r.category_id): float(r.amount) for r in budget_result.all()}

    actual_stmt = (
        select(
            Transaction.category_id,
            sa_func.sum(Transaction.amount).label("actual"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.date >= start,
            Transaction.date < end,
            Transaction.category_id.isnot(None),
        )
        .group_by(Transaction.category_id)
    )
    actual_result = await db.execute(actual_stmt)
    actuals = {str(r.category_id): float(r.actual) for r in actual_result.all()}

    cats_stmt = select(Category).where(
        Category.user_id == user_id,
        Category.category_type == CategoryType.EXPENSE,
    ).order_by(Category.sort_order)
    cats = (await db.execute(cats_stmt)).scalars().all()

    days_in_range = (end - start).days
    prorate = days_in_range / 30.0 if period == "week" else 1.0

    rows = []
    for cat in cats:
        cid = str(cat.id)
        raw_budget = budgets.get(cid, float(cat.budgeted_amount))
        budgeted = round(raw_budget * prorate, 2)
        actual = abs(actuals.get(cid, 0.0))
        rows.append({
            "category_id": cid,
            "category_name": cat.name,
            "budgeted": budgeted,
            "actual": actual,
            "variance": round(budgeted - actual, 2),
            "pct": round(actual / budgeted * 100, 1) if budgeted else 0,
        })
    return rows


async def category_averages(
    db: AsyncSession, user_id: uuid.UUID,
    periods: int = 6, period: str = "month",
) -> list[dict]:
    today = date.today()
    if period == "week":
        end = week_bounds(today)[0]
        start = end - timedelta(weeks=periods)
        divisor_label = "Weekly Avg"
    else:
        end = today.replace(day=1)
        m = end.month - periods
        y = end.year
        while m < 1:
            m += 12
            y -= 1
        start = date(y, m, 1)
        divisor_label = "Monthly Avg"

    stmt = (
        select(
            Category.id, Category.name,
            sa_func.sum(Transaction.amount).label("total"),
        )
        .outerjoin(Transaction, and_(
            Transaction.category_id == Category.id,
            Transaction.date >= start,
            Transaction.date < end,
        ))
        .where(Category.user_id == user_id, Category.category_type == CategoryType.EXPENSE)
        .group_by(Category.id)
        .order_by(Category.sort_order)
    )
    result = await db.execute(stmt)
    rows = []
    for r in result.all():
        total = float(abs(r.total or 0))
        rows.append({
            "category_id": str(r.id),
            "category_name": r.name,
            "total": total,
            "average": round(total / max(periods, 1), 2),
        })
    return rows, divisor_label


async def net_balance_history(
    db: AsyncSession, user_id: uuid.UUID,
    steps: int = 12, period: str = "month",
) -> list[dict]:
    """Periodic snapshots of total assets, liabilities, and net worth."""
    accounts = (await db.execute(
        select(Account).where(Account.user_id == user_id, Account.is_active.is_(True))
    )).scalars().all()

    today = date.today()
    history = []

    for i in range(steps - 1, -1, -1):
        if period == "week":
            ref = today - timedelta(weeks=i)
            _, snapshot_end = week_bounds(ref)
            label = (snapshot_end - timedelta(days=1)).strftime("%b %d")
        else:
            m = today.month - i
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            snapshot_end = date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)
            label = f"{y}-{m:02d}"

        assets = Decimal("0.00")
        liabilities = Decimal("0.00")

        for acct in accounts:
            tx_sum = (await db.execute(
                select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
                .where(Transaction.account_id == acct.id, Transaction.date < snapshot_end)
            )).scalar()
            balance = acct.initial_balance + tx_sum
            if acct.group == AccountGroup.ASSET:
                assets += balance
            else:
                liabilities += abs(balance)

        history.append({
            "label": label,
            "assets": float(assets),
            "liabilities": float(liabilities),
            "net_worth": float(assets - liabilities),
        })

    return history


async def spending_breakdown(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID, start: date, end: date,
) -> list[dict]:
    stmt = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.category_id == category_id,
            Transaction.date >= start,
            Transaction.date < end,
        )
        .order_by(Transaction.date)
    )
    result = await db.execute(stmt)
    return [
        {
            "id": str(tx.id),
            "date": tx.date.isoformat(),
            "amount": float(tx.amount),
            "description": tx.description,
        }
        for tx in result.scalars()
    ]
