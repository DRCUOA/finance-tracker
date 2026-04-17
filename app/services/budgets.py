import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, delete, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import Budget
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction


async def get_budget_suggestions(
    db: AsyncSession, user_id: uuid.UUID, periods: int = 3,
) -> list[dict]:
    """Return expense categories with their average monthly spend over the
    last *periods* full months, suitable for pre-filling a budget wizard."""
    today = date.today()
    end = today.replace(day=1)
    m = end.month - periods
    y = end.year
    while m < 1:
        m += 12
        y -= 1
    start = date(y, m, 1)

    cats_stmt = (
        select(Category)
        .where(
            Category.user_id == user_id,
            Category.category_type == CategoryType.EXPENSE,
            Category.parent_id.isnot(None),
        )
        .order_by(Category.sort_order)
    )
    cats = (await db.execute(cats_stmt)).scalars().all()

    if not cats:
        cats_stmt = (
            select(Category)
            .where(
                Category.user_id == user_id,
                Category.category_type == CategoryType.EXPENSE,
            )
            .order_by(Category.sort_order)
        )
        cats = (await db.execute(cats_stmt)).scalars().all()

    cat_ids = [c.id for c in cats]

    actual_stmt = (
        select(
            Transaction.category_id,
            sa_func.sum(Transaction.amount).label("total"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.category_id.in_(cat_ids),
            Transaction.date >= start,
            Transaction.date < end,
        )
        .group_by(Transaction.category_id)
    )
    actual_result = await db.execute(actual_stmt)
    totals = {
        r.category_id: float(abs(r.total)) if r.total and r.total < 0 else 0.0
        for r in actual_result.all()
    }

    budget_stmt = (
        select(Budget.category_id, Budget.amount)
        .where(
            Budget.user_id == user_id,
            Budget.year == today.year,
            Budget.month == today.month,
        )
    )
    overrides = {
        r.category_id: float(r.amount)
        for r in (await db.execute(budget_stmt)).all()
    }

    rows = []
    for cat in cats:
        total = totals.get(cat.id, 0.0)
        avg = round(total / max(periods, 1), 2)
        current_budget = float(cat.budgeted_amount)
        override = overrides.get(cat.id)

        rows.append({
            "id": str(cat.id),
            "name": cat.name,
            "parent_id": str(cat.parent_id) if cat.parent_id else None,
            "current_budget": current_budget,
            "month_override": override,
            "avg_spend": avg,
            "total_spend": round(total, 2),
            "is_fixed": cat.is_fixed,
        })

    return rows


async def get_income_average(
    db: AsyncSession, user_id: uuid.UUID, periods: int = 3,
) -> float:
    """Average monthly income over the last *periods* full months."""
    today = date.today()
    end = today.replace(day=1)
    m = end.month - periods
    y = end.year
    while m < 1:
        m += 12
        y -= 1
    start = date(y, m, 1)

    stmt = (
        select(sa_func.sum(Transaction.amount))
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Category.category_type == CategoryType.INCOME,
            Transaction.date >= start,
            Transaction.date < end,
            Transaction.amount > 0,
        )
    )
    total = (await db.execute(stmt)).scalar() or Decimal("0")
    return round(float(total) / max(periods, 1), 2)


async def get_income_categories(
    db: AsyncSession, user_id: uuid.UUID, periods: int = 3,
) -> list[dict]:
    """Return income categories with their average monthly income."""
    today = date.today()
    end = today.replace(day=1)
    m = end.month - periods
    y = end.year
    while m < 1:
        m += 12
        y -= 1
    start = date(y, m, 1)

    cats_stmt = (
        select(Category)
        .where(
            Category.user_id == user_id,
            Category.category_type == CategoryType.INCOME,
        )
        .order_by(Category.sort_order)
    )
    cats = (await db.execute(cats_stmt)).scalars().all()
    if not cats:
        return []

    cat_ids = [c.id for c in cats]
    actual_stmt = (
        select(
            Transaction.category_id,
            sa_func.sum(Transaction.amount).label("total"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.category_id.in_(cat_ids),
            Transaction.date >= start,
            Transaction.date < end,
            Transaction.amount > 0,
        )
        .group_by(Transaction.category_id)
    )
    totals = {
        r.category_id: float(r.total)
        for r in (await db.execute(actual_stmt)).all()
        if r.total
    }

    rows = []
    for cat in cats:
        total = totals.get(cat.id, 0.0)
        avg = round(total / max(periods, 1), 2)
        rows.append({
            "id": str(cat.id),
            "name": cat.name,
            "parent_id": str(cat.parent_id) if cat.parent_id else None,
            "avg_income": avg,
            "budgeted_amount": float(cat.budgeted_amount),
        })
    return rows


async def bulk_set_default_budgets(
    db: AsyncSession, user_id: uuid.UUID,
    budgets: dict[str, float],
) -> int:
    """Update Category.budgeted_amount for multiple categories at once.
    Returns count of categories updated."""
    count = 0
    for cat_id_str, amount in budgets.items():
        cat_id = uuid.UUID(cat_id_str)
        cat = await db.get(Category, cat_id)
        if not cat or cat.user_id != user_id:
            continue
        cat.budgeted_amount = Decimal(str(round(amount, 2)))
        count += 1
    await db.flush()
    return count


async def save_income_lines(
    db: AsyncSession, user_id: uuid.UUID,
    lines: list[dict],
) -> list[dict]:
    """Create or update income categories from the budget setup page.

    Each line has: id (str|None), name (str), amount (float).
    Returns list of {id, name} for the saved categories.
    """
    max_sort = 0
    existing_stmt = (
        select(sa_func.max(Category.sort_order))
        .where(Category.user_id == user_id, Category.category_type == CategoryType.INCOME)
    )
    max_sort = (await db.execute(existing_stmt)).scalar() or 0

    result = []
    for line in lines:
        name = line.get("name", "").strip()
        if not name:
            continue
        amount = Decimal(str(round(line.get("amount", 0), 2)))
        cat_id_str = line.get("id")

        cat = None
        if cat_id_str:
            try:
                cat_id = uuid.UUID(cat_id_str)
                cat = await db.get(Category, cat_id)
                if cat and cat.user_id != user_id:
                    cat = None
            except (ValueError, AttributeError):
                pass

        if cat:
            cat.name = name
            cat.budgeted_amount = amount
        else:
            max_sort += 1
            cat = Category(
                user_id=user_id,
                name=name,
                category_type=CategoryType.INCOME,
                budgeted_amount=amount,
                sort_order=max_sort,
            )
            db.add(cat)
            await db.flush()

        result.append({"id": str(cat.id), "name": cat.name})

    await db.flush()
    return result


async def set_month_override(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID, year: int, month: int,
    amount: Decimal,
) -> Budget:
    """Create or update a per-month budget override for a single category."""
    stmt = select(Budget).where(
        Budget.user_id == user_id,
        Budget.category_id == category_id,
        Budget.year == year,
        Budget.month == month,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.amount = amount
        await db.flush()
        return existing

    budget = Budget(
        user_id=user_id,
        category_id=category_id,
        year=year,
        month=month,
        amount=amount,
    )
    db.add(budget)
    await db.flush()
    return budget


async def clear_month_override(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID, year: int, month: int,
) -> bool:
    """Remove a per-month override, reverting to the category default."""
    stmt = delete(Budget).where(
        Budget.user_id == user_id,
        Budget.category_id == category_id,
        Budget.year == year,
        Budget.month == month,
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0


async def copy_budgets_from_month(
    db: AsyncSession, user_id: uuid.UUID,
    source_year: int, source_month: int,
    target_year: int, target_month: int,
) -> int:
    """Copy all budget overrides from one month to another. Returns count."""
    source_stmt = select(Budget).where(
        Budget.user_id == user_id,
        Budget.year == source_year,
        Budget.month == source_month,
    )
    source_rows = (await db.execute(source_stmt)).scalars().all()
    if not source_rows:
        return 0

    count = 0
    for src in source_rows:
        existing = (await db.execute(
            select(Budget).where(
                Budget.user_id == user_id,
                Budget.category_id == src.category_id,
                Budget.year == target_year,
                Budget.month == target_month,
            )
        )).scalar_one_or_none()

        if existing:
            existing.amount = src.amount
        else:
            db.add(Budget(
                user_id=user_id,
                category_id=src.category_id,
                year=target_year,
                month=target_month,
                amount=src.amount,
            ))
        count += 1
    await db.flush()
    return count


async def get_month_overrides(
    db: AsyncSession, user_id: uuid.UUID,
    year: int, month: int,
) -> dict[str, float]:
    """Return all budget overrides for a given month as {category_id: amount}."""
    stmt = select(Budget.category_id, Budget.amount).where(
        Budget.user_id == user_id,
        Budget.year == year,
        Budget.month == month,
    )
    result = await db.execute(stmt)
    return {str(r.category_id): float(r.amount) for r in result.all()}


async def first_budget_month_start(
    db: AsyncSession, user_id: uuid.UUID,
) -> date | None:
    """First calendar day of the earliest month that has a budget row."""
    row = (await db.execute(
        select(Budget.year, Budget.month)
        .where(Budget.user_id == user_id)
        .order_by(Budget.year, Budget.month)
        .limit(1),
    )).first()
    if not row:
        return None
    return date(int(row.year), int(row.month), 1)
