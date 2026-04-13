import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, func as sa_func, extract, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, ACCOUNT_TYPE_GROUPS, AccountGroup
from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.statement import Statement
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
    account_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Income vs expenses for an arbitrary date range, broken down by category."""
    join_conditions = [
        Transaction.category_id == Category.id,
        Transaction.date >= start,
        Transaction.date < end,
    ]
    if account_ids is not None:
        join_conditions.append(Transaction.account_id.in_(account_ids))

    stmt = (
        select(
            Category.id,
            Category.name,
            Category.category_type,
            Category.parent_id,
            sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")).label("total"),
            sa_func.coalesce(
                sa_func.sum(case((Transaction.amount > 0, Transaction.amount), else_=Decimal("0.00"))),
                Decimal("0.00"),
            ).label("pos_total"),
            sa_func.coalesce(
                sa_func.sum(case((Transaction.amount < 0, Transaction.amount), else_=Decimal("0.00"))),
                Decimal("0.00"),
            ).label("neg_total"),
        )
        .outerjoin(Transaction, and_(*join_conditions))
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
        if row.category_type != CategoryType.TRANSFER:
            income += row.pos_total or Decimal("0.00")
            expenses += abs(row.neg_total or Decimal("0.00"))

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
    account_ids: list[uuid.UUID] | None = None,
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
    if account_ids is not None:
        actual_stmt = actual_stmt.where(Transaction.account_id.in_(account_ids))
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
        raw_actual = actuals.get(cid, 0.0)
        actual = abs(raw_actual) if raw_actual < 0 else 0.0
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
    account_ids: list[uuid.UUID] | None = None,
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

    join_conditions = [
        Transaction.category_id == Category.id,
        Transaction.date >= start,
        Transaction.date < end,
    ]
    if account_ids is not None:
        join_conditions.append(Transaction.account_id.in_(account_ids))

    stmt = (
        select(
            Category.id, Category.name,
            sa_func.sum(Transaction.amount).label("total"),
        )
        .outerjoin(Transaction, and_(*join_conditions))
        .where(Category.user_id == user_id, Category.category_type == CategoryType.EXPENSE)
        .group_by(Category.id)
        .order_by(Category.sort_order)
    )
    result = await db.execute(stmt)
    rows = []
    for r in result.all():
        raw = float(r.total or 0)
        total = abs(raw) if raw < 0 else 0.0
        rows.append({
            "category_id": str(r.id),
            "category_name": r.name,
            "total": total,
            "average": round(total / max(periods, 1), 2),
        })
    return rows, divisor_label


async def transaction_date_range(
    db: AsyncSession, user_id: uuid.UUID,
    account_ids: list[uuid.UUID] | None = None,
) -> tuple[date | None, date | None]:
    """Return (oldest, newest) transaction dates for the user."""
    stmt = select(
        sa_func.min(Transaction.date),
        sa_func.max(Transaction.date),
    ).where(Transaction.user_id == user_id)
    if account_ids is not None:
        stmt = stmt.where(Transaction.account_id.in_(account_ids))
    row = (await db.execute(stmt)).one()
    return row[0], row[1]


def span_to_steps(
    span: str, period: str,
    oldest: date | None = None, ref: date | None = None,
) -> int:
    """Translate a time-span key + period into the number of chart steps."""
    fixed = {
        "6m":  {"month": 6,  "week": 26},
        "1y":  {"month": 12, "week": 52},
        "5y":  {"month": 60, "week": 260},
    }
    if span in fixed:
        return fixed[span][period]

    if span == "all" and oldest:
        anchor = ref or date.today()
        if period == "week":
            return max(((anchor - oldest).days // 7) + 1, 2)
        months = (anchor.year - oldest.year) * 12 + (anchor.month - oldest.month) + 1
        return max(months, 2)

    return 1


async def net_balance_history(
    db: AsyncSession, user_id: uuid.UUID,
    steps: int = 12, period: str = "month",
    ref_date: date | None = None,
    account_ids: list[uuid.UUID] | None = None,
) -> list[dict]:
    """Periodic snapshots of total assets, liabilities, and net worth."""
    stmt = select(Account).where(Account.user_id == user_id, Account.is_active.is_(True))
    if account_ids is not None:
        stmt = stmt.where(Account.id.in_(account_ids))
    accounts = (await db.execute(stmt)).scalars().all()

    anchor = ref_date or date.today()
    history = []

    for i in range(steps - 1, -1, -1):
        if period == "week":
            ref = anchor - timedelta(weeks=i)
            _, snapshot_end = week_bounds(ref)
            label = (snapshot_end - timedelta(days=1)).strftime("%b %d")
        else:
            m = anchor.month - i
            y = anchor.year
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


def _months_between(start: date, end: date) -> set[tuple[int, int]]:
    """Return the set of (year, month) tuples covering start..end inclusive."""
    periods: set[tuple[int, int]] = set()
    yr, mo = start.year, start.month
    end_ym = (end.year, end.month)
    while (yr, mo) <= end_ym:
        periods.add((yr, mo))
        mo += 1
        if mo > 12:
            mo = 1
            yr += 1
    return periods


async def import_coverage(
    db: AsyncSession, user_id: uuid.UUID,
) -> dict:
    """Build a month-by-account matrix showing import coverage.

    Cell values: positive int = transaction count, 0 = covered by an import
    but no transactions, -1 = not covered by any import.

    Returns {"months": [...], "accounts": [{"name": ..., "cells": [...]}]}
    """
    tx_stmt = (
        select(
            Transaction.account_id,
            extract("year", Transaction.date).label("yr"),
            extract("month", Transaction.date).label("mo"),
            sa_func.count().label("cnt"),
        )
        .where(Transaction.user_id == user_id)
        .group_by(Transaction.account_id, "yr", "mo")
    )
    tx_result = await db.execute(tx_stmt)
    tx_rows = tx_result.all()

    stmt_stmt = (
        select(Statement.account_id, Statement.start_date, Statement.end_date)
        .where(
            Statement.user_id == user_id,
            Statement.status == "imported",
            Statement.start_date.isnot(None),
            Statement.end_date.isnot(None),
        )
    )
    stmt_result = await db.execute(stmt_stmt)
    stmt_rows = stmt_result.all()

    if not tx_rows and not stmt_rows:
        return {"months": [], "accounts": []}

    acct_stmt = (
        select(Account)
        .where(Account.user_id == user_id, Account.is_active.is_(True))
        .order_by(Account.name)
    )
    accounts = (await db.execute(acct_stmt)).scalars().all()

    bucket: dict[uuid.UUID, dict[tuple[int, int], int]] = {}
    all_periods: set[tuple[int, int]] = set()
    for r in tx_rows:
        yr, mo = int(r.yr), int(r.mo)
        all_periods.add((yr, mo))
        bucket.setdefault(r.account_id, {})[(yr, mo)] = r.cnt

    covered: dict[uuid.UUID, set[tuple[int, int]]] = {}
    for s in stmt_rows:
        periods = _months_between(s.start_date, s.end_date)
        all_periods |= periods
        covered.setdefault(s.account_id, set()).update(periods)

    if not all_periods:
        return {"months": [], "accounts": []}

    min_period = min(all_periods)
    today = date.today()
    max_period = max(max(all_periods), (today.year, today.month))

    months: list[tuple[int, int]] = []
    yr, mo = max_period
    while (yr, mo) >= min_period:
        months.append((yr, mo))
        mo -= 1
        if mo < 1:
            mo = 12
            yr -= 1

    month_labels = [date(y, m, 1).strftime("%b %y") for y, m in months]

    account_rows = []
    for acct in accounts:
        acct_tx = bucket.get(acct.id, {})
        acct_cov = covered.get(acct.id, set())
        cells = []
        for p in months:
            if p in acct_tx:
                cells.append(acct_tx[p])
            elif p in acct_cov:
                cells.append(0)
            else:
                cells.append(-1)
        if any(c >= 0 for c in cells):
            account_rows.append({"name": acct.name, "cells": cells})

    return {"months": month_labels, "accounts": account_rows}


async def category_spending_trend(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID,
    periods: int = 12,
    ref_date: date | None = None,
    account_ids: list[uuid.UUID] | None = None,
) -> dict | None:
    """Monthly spending trend for a single category over *periods* months."""
    anchor = ref_date or date.today()

    cat = (await db.execute(
        select(Category).where(Category.id == category_id, Category.user_id == user_id)
    )).scalar_one_or_none()
    if not cat:
        return None

    month_ranges: list[tuple[date, date, str]] = []
    for i in range(periods - 1, -1, -1):
        m = anchor.month - i
        y = anchor.year
        while m <= 0:
            m += 12
            y -= 1
        s = date(y, m, 1)
        e = date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)
        month_ranges.append((s, e, s.strftime("%b %y")))

    conditions = [
        Transaction.user_id == user_id,
        Transaction.category_id == category_id,
        Transaction.amount < 0,
    ]
    if account_ids is not None:
        conditions.append(Transaction.account_id.in_(account_ids))

    stmt = (
        select(
            extract("year", Transaction.date).label("yr"),
            extract("month", Transaction.date).label("mo"),
            sa_func.sum(Transaction.amount).label("total"),
            sa_func.count().label("cnt"),
        )
        .where(*conditions)
        .group_by("yr", "mo")
    )
    result = await db.execute(stmt)
    monthly = {
        (int(r.yr), int(r.mo)): {"total": float(abs(r.total)), "count": r.cnt}
        for r in result.all()
    }

    labels = []
    amounts = []
    for s, _e, lbl in month_ranges:
        labels.append(lbl)
        amounts.append(monthly.get((s.year, s.month), {"total": 0.0})["total"])

    tx_stmt = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.category_id == category_id,
            Transaction.date >= month_ranges[0][0],
            Transaction.date < month_ranges[-1][1],
        )
        .order_by(Transaction.date.desc())
        .limit(50)
    )
    if account_ids is not None:
        tx_stmt = tx_stmt.where(Transaction.account_id.in_(account_ids))
    txs = (await db.execute(tx_stmt)).scalars().all()

    total = sum(amounts)
    return {
        "category_name": cat.name,
        "labels": labels,
        "amounts": [round(a, 2) for a in amounts],
        "total": round(total, 2),
        "average": round(total / max(periods, 1), 2),
        "transactions": [
            {
                "id": str(tx.id),
                "date": tx.date.isoformat(),
                "amount": float(tx.amount),
                "description": tx.description,
                "is_cleared": tx.is_cleared,
            }
            for tx in txs
        ],
    }


async def category_transactions_detail(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID,
    date_from: date | None = None,
    date_to: date | None = None,
    account_ids: list[uuid.UUID] | None = None,
) -> dict | None:
    """Detailed transactions and summary for a single category (deepdive modal)."""
    from sqlalchemy.orm import selectinload

    cat = (await db.execute(
        select(Category).where(Category.id == category_id, Category.user_id == user_id)
    )).scalar_one_or_none()
    if not cat:
        return None

    conditions = [
        Transaction.user_id == user_id,
        Transaction.category_id == category_id,
    ]
    if date_from:
        conditions.append(Transaction.date >= date_from)
    if date_to:
        conditions.append(Transaction.date <= date_to)
    if account_ids is not None:
        conditions.append(Transaction.account_id.in_(account_ids))

    tx_stmt = (
        select(Transaction)
        .options(selectinload(Transaction.account))
        .where(*conditions)
        .order_by(Transaction.date.desc())
    )
    txs = (await db.execute(tx_stmt)).scalars().all()

    total = sum(float(tx.amount) for tx in txs)
    count = len(txs)
    months = set()
    amounts = []
    for tx in txs:
        months.add((tx.date.year, tx.date.month))
        amounts.append(float(tx.amount))
    num_months = max(len(months), 1)

    return {
        "category_name": cat.name,
        "category_type": cat.category_type.value,
        "summary": {
            "total": round(total, 2),
            "count": count,
            "avg_monthly": round(total / num_months, 2),
            "min_amount": round(min(amounts), 2) if amounts else 0,
            "max_amount": round(max(amounts), 2) if amounts else 0,
            "num_months": num_months,
        },
        "transactions": [
            {
                "id": str(tx.id),
                "date": tx.date.isoformat(),
                "description": tx.description,
                "amount": float(tx.amount),
                "account_name": tx.account.name if tx.account else "",
                "reference": tx.reference or "",
                "is_cleared": tx.is_cleared,
            }
            for tx in txs
        ],
    }


async def income_vs_spending_trend(
    db: AsyncSession, user_id: uuid.UUID,
    periods: int = 6, period: str = "month",
    account_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Multi-period income/expenses/net trend with rolling 3-period average."""
    today = date.today()
    data_points: list[dict] = []

    for i in range(periods - 1, -1, -1):
        ref = step_period(today, -i, period)
        start, end = period_bounds(ref, period)
        label = period_label(ref, period) if period == "week" else ref.strftime("%b %y")

        conditions = [
            Transaction.category_id == Category.id,
            Transaction.date >= start,
            Transaction.date < end,
        ]
        if account_ids is not None:
            conditions.append(Transaction.account_id.in_(account_ids))

        stmt = (
            select(
                Category.category_type,
                sa_func.coalesce(
                    sa_func.sum(case((Transaction.amount > 0, Transaction.amount), else_=Decimal("0.00"))),
                    Decimal("0.00"),
                ).label("pos"),
                sa_func.coalesce(
                    sa_func.sum(case((Transaction.amount < 0, Transaction.amount), else_=Decimal("0.00"))),
                    Decimal("0.00"),
                ).label("neg"),
            )
            .outerjoin(Transaction, and_(*conditions))
            .where(Category.user_id == user_id, Category.category_type != CategoryType.TRANSFER)
            .group_by(Category.category_type)
        )
        result = await db.execute(stmt)
        income = Decimal("0.00")
        expenses = Decimal("0.00")
        for row in result.all():
            income += row.pos or Decimal("0.00")
            expenses += abs(row.neg or Decimal("0.00"))

        data_points.append({
            "label": label,
            "income": float(income),
            "expenses": float(expenses),
            "net": float(income - expenses),
        })

    for i, dp in enumerate(data_points):
        window = data_points[max(0, i - 2):i + 1]
        dp["rolling_avg_net"] = round(sum(p["net"] for p in window) / len(window), 2)

    return {
        "points": data_points,
        "total_income": sum(p["income"] for p in data_points),
        "total_expenses": sum(p["expenses"] for p in data_points),
        "total_net": sum(p["net"] for p in data_points),
    }


async def spending_by_category_comparison(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
    period: str = "month",
    account_ids: list[uuid.UUID] | None = None,
) -> list[dict]:
    """Per-category spending with % of total and prior period comparison."""
    prev_ref = step_period(start, -1, period)
    prev_start, prev_end = period_bounds(prev_ref, period)

    async def _category_totals(s: date, e: date) -> dict[str, float]:
        conditions = [
            Transaction.user_id == user_id,
            Transaction.category_id.isnot(None),
            Transaction.date >= s,
            Transaction.date < e,
            Transaction.amount < 0,
        ]
        if account_ids is not None:
            conditions.append(Transaction.account_id.in_(account_ids))
        stmt = (
            select(
                Transaction.category_id,
                sa_func.sum(Transaction.amount).label("total"),
            )
            .where(*conditions)
            .group_by(Transaction.category_id)
        )
        result = await db.execute(stmt)
        return {str(r.category_id): float(abs(r.total)) for r in result.all()}

    current = await _category_totals(start, end)
    previous = await _category_totals(prev_start, prev_end)

    cats_stmt = select(Category).where(
        Category.user_id == user_id,
        Category.category_type == CategoryType.EXPENSE,
    ).order_by(Category.sort_order)
    cats = (await db.execute(cats_stmt)).scalars().all()

    grand_total = sum(current.values()) or 1.0

    rows = []
    for cat in cats:
        cid = str(cat.id)
        curr_val = current.get(cid, 0.0)
        prev_val = previous.get(cid, 0.0)
        if curr_val == 0 and prev_val == 0:
            continue
        change = curr_val - prev_val
        rows.append({
            "category_id": cid,
            "category_name": cat.name,
            "is_fixed": cat.is_fixed,
            "current": round(curr_val, 2),
            "previous": round(prev_val, 2),
            "change": round(change, 2),
            "change_pct": round(change / prev_val * 100, 1) if prev_val else 0,
            "pct_of_total": round(curr_val / grand_total * 100, 1),
        })
    rows.sort(key=lambda r: r["current"], reverse=True)
    return rows


async def fixed_vs_flexible_summary(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
    account_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Split spending and income into fixed vs flexible categories."""
    conditions = [
        Transaction.category_id == Category.id,
        Transaction.date >= start,
        Transaction.date < end,
    ]
    if account_ids is not None:
        conditions.append(Transaction.account_id.in_(account_ids))

    stmt = (
        select(
            Category.id,
            Category.name,
            Category.category_type,
            Category.is_fixed,
            sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")).label("total"),
        )
        .outerjoin(Transaction, and_(*conditions))
        .where(Category.user_id == user_id, Category.category_type != CategoryType.TRANSFER)
        .group_by(Category.id)
        .order_by(Category.sort_order)
    )
    result = await db.execute(stmt)

    income = Decimal("0.00")
    fixed_items: list[dict] = []
    flexible_items: list[dict] = []
    fixed_total = Decimal("0.00")
    flexible_total = Decimal("0.00")

    for row in result.all():
        total = row.total or Decimal("0.00")
        if row.category_type == CategoryType.INCOME:
            income += max(total, Decimal("0.00"))
            continue
        spent = abs(min(total, Decimal("0.00")))
        if spent == 0:
            continue
        item = {
            "category_id": str(row.id),
            "category_name": row.name,
            "amount": float(spent),
            "is_fixed": row.is_fixed,
        }
        if row.is_fixed:
            fixed_items.append(item)
            fixed_total += spent
        else:
            flexible_items.append(item)
            flexible_total += spent

    total_spending = fixed_total + flexible_total
    income_f = float(income) or 1.0

    return {
        "income": float(income),
        "fixed_total": float(fixed_total),
        "flexible_total": float(flexible_total),
        "total_spending": float(total_spending),
        "fixed_pct_income": round(float(fixed_total) / income_f * 100, 1),
        "flexible_pct_income": round(float(flexible_total) / income_f * 100, 1),
        "remaining_buffer": float(income - total_spending),
        "remaining_pct": round(float(income - total_spending) / income_f * 100, 1),
        "fixed_items": sorted(fixed_items, key=lambda x: x["amount"], reverse=True),
        "flexible_items": sorted(flexible_items, key=lambda x: x["amount"], reverse=True),
    }


async def cashflow_trend(
    db: AsyncSession, user_id: uuid.UUID,
    periods: int = 12,
    account_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Monthly cashflow: opening/closing balances, net flow, rolling trend, buffer."""
    acct_stmt = select(Account).where(
        Account.user_id == user_id, Account.is_active.is_(True), Account.is_cashflow.is_(True),
    )
    if account_ids is not None:
        acct_stmt = acct_stmt.where(Account.id.in_(account_ids))
    accounts = (await db.execute(acct_stmt)).scalars().all()

    today = date.today()
    points: list[dict] = []

    for i in range(periods - 1, -1, -1):
        ref = step_period(today, -i, "month")
        m_start, m_end = month_bounds(ref.year, ref.month)
        label = ref.strftime("%b %y")

        opening = Decimal("0.00")
        closing = Decimal("0.00")

        for acct in accounts:
            open_tx = (await db.execute(
                select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
                .where(Transaction.account_id == acct.id, Transaction.date < m_start)
            )).scalar()
            close_tx = (await db.execute(
                select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
                .where(Transaction.account_id == acct.id, Transaction.date < m_end)
            )).scalar()
            opening += acct.initial_balance + open_tx
            closing += acct.initial_balance + close_tx

        net_flow = closing - opening
        points.append({
            "label": label,
            "opening": float(opening),
            "closing": float(closing),
            "net_flow": float(net_flow),
        })

    for i, dp in enumerate(points):
        window = points[max(0, i - 2):i + 1]
        dp["rolling_net"] = round(sum(p["net_flow"] for p in window) / len(window), 2)

    latest_closing = points[-1]["closing"] if points else 0.0
    recent_expenses = []
    for p in points[-3:]:
        ref_d = date.today()
        for j in range(len(points) - 1, -1, -1):
            if points[j] is p:
                ref_d = step_period(today, -(len(points) - 1 - j), "month")
                break
        ms, me = month_bounds(ref_d.year, ref_d.month)
        conditions = [
            Transaction.user_id == user_id,
            Transaction.date >= ms,
            Transaction.date < me,
            Transaction.amount < 0,
        ]
        if account_ids is not None:
            conditions.append(Transaction.account_id.in_(account_ids))
        exp = (await db.execute(
            select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
            .where(*conditions)
        )).scalar()
        recent_expenses.append(float(abs(exp)))

    avg_monthly_expenses = sum(recent_expenses) / max(len(recent_expenses), 1)
    avg_daily_expenses = avg_monthly_expenses / 30.0
    days_buffer = round(latest_closing / avg_daily_expenses, 0) if avg_daily_expenses > 0 else 0

    return {
        "points": points,
        "latest_balance": latest_closing,
        "avg_monthly_expenses": round(avg_monthly_expenses, 2),
        "days_of_buffer": int(days_buffer),
    }


async def spending_breakdown(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID, start: date, end: date,
    account_ids: list[uuid.UUID] | None = None,
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
    if account_ids is not None:
        stmt = stmt.where(Transaction.account_id.in_(account_ids))
    result = await db.execute(stmt)
    return [
        {
            "id": str(tx.id),
            "date": tx.date.isoformat(),
            "amount": float(tx.amount),
            "description": tx.description,
            "is_cleared": tx.is_cleared,
        }
        for tx in result.scalars()
    ]


# ---------------------------------------------------------------------------
# Spending Pulse — Live Position
# ---------------------------------------------------------------------------

def _spending_status(pct: float) -> str:
    if pct > 100:
        return "over"
    if pct > 90:
        return "tight"
    if pct > 70:
        return "watch"
    return "on_track"


_HERO_MESSAGES = {
    "current": {
        "on_track": "You\u2019re in a good position \u2014 comfortably within your {period} means",
        "watch": "Worth keeping an eye on \u2014 commitments are eating into your buffer",
        "tight": "Getting tight \u2014 most of your {period} cash is spoken for",
        "over": "You\u2019re overcommitted for this {period} \u2014 review what can shift",
    },
    "past": {
        "on_track": "This {period} closed under budget \u2014 well managed",
        "watch": "This {period} came in near the limit \u2014 not much room was left",
        "tight": "This {period} ran tight \u2014 nearly everything was spoken for",
        "over": "This {period} went over budget \u2014 more was spent than allowed",
    },
    "future": {
        "on_track": "Looking comfortable \u2014 commitments are well within the {period} budget",
        "watch": "Commitments are starting to fill this {period}\u2019s allowance",
        "tight": "This {period} is filling up fast \u2014 most of the budget is pre-committed",
        "over": "This {period} is already overcommitted before it starts",
    },
}


def _period_phase(start: date, end: date, today: date) -> str:
    if today >= end:
        return "past"
    if today < start:
        return "future"
    return "current"


async def weekly_spending_pulse(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
    period: str = "week",
    account_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Live position data: actuals + commitments + reserves."""
    from app.services.commitments import (
        commitment_totals_for_period, commitments_by_category,
        project_recurring_commitments,
    )

    today = date.today()
    phase = _period_phase(start, end, today)
    total_days = (end - start).days
    days_elapsed = max(min((today - start).days + 1, total_days), 1)
    days_remaining = max(total_days - days_elapsed, 0)

    # Budget overrides
    by, bm = start.year, start.month
    budget_result = await db.execute(
        select(Budget.category_id, Budget.amount)
        .where(Budget.user_id == user_id, Budget.year == by, Budget.month == bm)
    )
    budgets = {str(r.category_id): float(r.amount) for r in budget_result.all()}

    # Categories
    cats = (await db.execute(
        select(Category).where(
            Category.user_id == user_id,
            Category.category_type == CategoryType.EXPENSE,
        ).order_by(Category.sort_order)
    )).scalars().all()

    prorate = total_days / 30.0 if period == "week" else 1.0

    # Actuals by category — expense categories only, signed net so refunds
    # offset spend.  The sum is negative for net outflows; abs() gives the
    # positive "consumed" figure.  Clamp to zero so a net-refund category
    # never produces a negative "actual spent".
    expense_cat_ids = [c.id for c in cats]
    actual_stmt = (
        select(
            Transaction.category_id,
            sa_func.sum(Transaction.amount).label("actual"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.date >= start,
            Transaction.date < end,
            Transaction.category_id.in_(expense_cat_ids),
        )
        .group_by(Transaction.category_id)
    )
    if account_ids is not None:
        actual_stmt = actual_stmt.where(Transaction.account_id.in_(account_ids))
    actuals = {
        str(r.category_id): max(float(abs(r.actual)), 0.0)
        for r in (await db.execute(actual_stmt)).all()
        if r.actual and r.actual < 0
    }

    # Commitments — use the actual view period so clearing a commitment
    # is immediately reflected.  Recurring projection still covers the
    # enclosing month to ensure future instances exist.
    _, m_end = month_bounds(start.year, start.month)
    await project_recurring_commitments(db, user_id, m_end)

    commit_totals_raw = await commitment_totals_for_period(db, user_id, start, end)
    commit_by_cat_raw = await commitments_by_category(db, user_id, start, end)

    commit_totals = {
        "committed_out": round(commit_totals_raw["committed_out"], 2),
        "committed_in": round(commit_totals_raw["committed_in"], 2),
    }
    commit_by_cat = {
        cid: round(v, 2) for cid, v in commit_by_cat_raw.items()
    }

    # Build per-category rows with live position
    period_budget = 0.0
    total_reserved = 0.0
    cat_rows = []
    for cat in cats:
        cid = str(cat.id)
        raw_budget = budgets.get(cid, float(cat.budgeted_amount))
        budgeted = round(raw_budget * prorate, 2)
        actual = actuals.get(cid, 0.0)
        committed = commit_by_cat.get(cid, 0.0)

        # Reserve: for categories with reserve_amount, the minimum "spoken for"
        # is the prorated reserve even if actual+committed is lower
        reserve_raw = float(cat.reserve_amount) * prorate
        reserve = round(reserve_raw, 2)
        # "spoken for" = max(actual + committed, reserve) for reserved categories
        # For non-reserved categories, spoken_for = actual + committed
        if reserve > 0:
            spoken_for = round(max(actual + committed, reserve), 2)
            reserve_gap = round(max(reserve - actual - committed, 0), 2)
        else:
            spoken_for = round(actual + committed, 2)
            reserve_gap = 0.0

        total_reserved += reserve_gap
        period_budget += budgeted

        pct = round(spoken_for / budgeted * 100, 1) if budgeted else (100.0 if spoken_for > 0 else 0.0)

        cat_rows.append({
            "category_id": cid,
            "category_name": cat.name,
            "budgeted": budgeted,
            "actual": round(actual, 2),
            "committed": round(committed, 2),
            "reserve": reserve,
            "reserve_gap": reserve_gap,
            "spoken_for": spoken_for,
            "pct": pct,
            "status": _spending_status(pct),
            "variance": round(budgeted - spoken_for, 2),
        })
    cat_rows.sort(key=lambda r: r["pct"], reverse=True)

    total_actual = sum(r["actual"] for r in cat_rows)
    total_committed = commit_totals["committed_out"]
    total_spoken_for = round(total_actual + total_committed + total_reserved, 2)

    live_available = round(period_budget - total_spoken_for, 2)
    overall_pct = round(total_spoken_for / period_budget * 100, 1) if period_budget else 0.0
    status = _spending_status(overall_pct)

    safe_to_spend = round(max(live_available, 0) / max(days_remaining, 1), 2)

    budget_daily_rate = round(period_budget / max(total_days, 1), 2)
    elapsed_budget = budget_daily_rate * days_elapsed
    pace_pct = round(total_actual / elapsed_budget * 100, 1) if elapsed_budget else 0.0

    # Day-by-day expense-category spend (signed net so refunds reduce the bar).
    # Only expense-type categories; transfers/income excluded.
    daily_stmt = (
        select(
            Transaction.date,
            sa_func.sum(Transaction.amount).label("total"),
        )
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.date >= start,
            Transaction.date < end,
            Category.category_type == CategoryType.EXPENSE,
        )
        .group_by(Transaction.date)
        .order_by(Transaction.date)
    )
    if account_ids is not None:
        daily_stmt = daily_stmt.where(Transaction.account_id.in_(account_ids))
    daily_result = await db.execute(daily_stmt)
    daily_map = {
        r.date: max(float(abs(r.total)), 0.0)
        for r in daily_result.all()
        if r.total and r.total < 0
    }

    daily_spending = []
    for i in range(total_days):
        d = start + timedelta(days=i)
        daily_spending.append({
            "date": d.isoformat(),
            "label": d.strftime("%a"),
            "amount": round(daily_map.get(d, 0.0), 2),
        })

    period_word = "week" if period == "week" else "month"

    return {
        "period_budget": round(period_budget, 2),
        "total_actual": round(total_actual, 2),
        "total_committed": round(total_committed, 2),
        "total_reserved": round(total_reserved, 2),
        "total_spoken_for": total_spoken_for,
        "committed_in": round(commit_totals["committed_in"], 2),
        "live_available": live_available,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "days_total": total_days,
        "safe_to_spend": safe_to_spend,
        "pace_pct": pace_pct,
        "overall_pct": overall_pct,
        "status": status,
        "period_phase": phase,
        "message": _HERO_MESSAGES[phase][status].format(period=period_word),
        "daily_spending": daily_spending,
        "budget_daily_rate": budget_daily_rate,
        "categories": cat_rows,
    }


async def spending_category_transactions(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID,
    start: date, end: date,
    account_ids: list[uuid.UUID] | None = None,
) -> list[dict] | None:
    """Transactions for a single category within a period (for HTMX drill-down)."""
    cat = (await db.execute(
        select(Category).where(Category.id == category_id, Category.user_id == user_id)
    )).scalar_one_or_none()
    if not cat:
        return None

    conditions = [
        Transaction.user_id == user_id,
        Transaction.category_id == category_id,
        Transaction.date >= start,
        Transaction.date < end,
    ]
    if account_ids is not None:
        conditions.append(Transaction.account_id.in_(account_ids))

    txs = (await db.execute(
        select(Transaction).where(*conditions).order_by(Transaction.date.desc())
    )).scalars().all()

    return [
        {
            "id": str(tx.id),
            "date": tx.date.isoformat(),
            "description": tx.description,
            "amount": float(tx.amount),
            "is_cleared": tx.is_cleared,
        }
        for tx in txs
    ]


async def rolling_over_under(
    db: AsyncSession,
    user_id: uuid.UUID,
    rolling_start: date,
    period_end: date,
    account_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Cumulative budget vs expense-category spend from *rolling_start* through
    the end of the period containing *period_end*.

    Returns per-month rows and a running total so the template can show the
    cumulative over/under position.
    """
    # Enumerate each month from rolling_start to period_end
    months: list[tuple[date, date]] = []
    y, m = rolling_start.year, rolling_start.month
    while True:
        ms, me = month_bounds(y, m)
        months.append((ms, me))
        if me >= period_end:
            break
        m += 1
        if m > 12:
            m = 1
            y += 1

    # Expense categories
    cats = (await db.execute(
        select(Category).where(
            Category.user_id == user_id,
            Category.category_type == CategoryType.EXPENSE,
        )
    )).scalars().all()
    expense_cat_ids = [c.id for c in cats]
    cat_default_budgets = {c.id: float(c.budgeted_amount) for c in cats}

    # Budget overrides across the whole range
    budget_rows = (await db.execute(
        select(Budget.category_id, Budget.year, Budget.month, Budget.amount)
        .where(Budget.user_id == user_id)
    )).all()
    budget_lookup: dict[tuple[int, int], dict[str, float]] = {}
    for br in budget_rows:
        key = (br.year, br.month)
        budget_lookup.setdefault(key, {})[str(br.category_id)] = float(br.amount)

    # Actual expense spend across the full range (signed net)
    actual_stmt = (
        select(
            extract("year", Transaction.date).label("yr"),
            extract("month", Transaction.date).label("mo"),
            sa_func.sum(Transaction.amount).label("total"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.date >= rolling_start,
            Transaction.date < months[-1][1],
            Transaction.category_id.in_(expense_cat_ids),
        )
        .group_by("yr", "mo")
    )
    if account_ids is not None:
        actual_stmt = actual_stmt.where(Transaction.account_id.in_(account_ids))
    actual_rows = (await db.execute(actual_stmt)).all()
    actual_by_month = {
        (int(r.yr), int(r.mo)): max(float(abs(r.total)), 0.0) if r.total and r.total < 0 else 0.0
        for r in actual_rows
    }

    today = date.today()
    running_total = 0.0
    month_rows = []
    for ms, me in months:
        ym = (ms.year, ms.month)
        overrides = budget_lookup.get(ym, {})
        month_budget = sum(
            overrides.get(str(cid), cat_default_budgets[cid])
            for cid in expense_cat_ids
        )
        month_spend = actual_by_month.get(ym, 0.0)
        variance = round(month_budget - month_spend, 2)
        running_total = round(running_total + variance, 2)

        phase = _period_phase(ms, me, today)

        month_rows.append({
            "year": ms.year,
            "month": ms.month,
            "label": ms.strftime("%b %Y"),
            "budget": round(month_budget, 2),
            "spent": round(month_spend, 2),
            "variance": variance,
            "running_total": running_total,
            "phase": phase,
        })

    return {
        "rolling_start": rolling_start.isoformat(),
        "period_end": period_end.isoformat(),
        "cumulative_over_under": running_total,
        "months": month_rows,
    }
