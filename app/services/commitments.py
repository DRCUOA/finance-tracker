import calendar
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, and_
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.commitment import (
    Commitment, CommitmentConfidence, CommitmentDirection, CommitmentRecurrence,
)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _add_months(d: date, months: int) -> date:
    m = d.month + months
    y = d.year
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    max_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, max_day))


def _next_due_date(current: date, recurrence: CommitmentRecurrence) -> date:
    if recurrence == CommitmentRecurrence.WEEKLY:
        return current + timedelta(weeks=1)
    if recurrence == CommitmentRecurrence.FORTNIGHTLY:
        return current + timedelta(weeks=2)
    if recurrence == CommitmentRecurrence.MONTHLY:
        return _add_months(current, 1)
    if recurrence == CommitmentRecurrence.QUARTERLY:
        return _add_months(current, 3)
    if recurrence == CommitmentRecurrence.ANNUALLY:
        return _add_months(current, 12)
    return current


# ---------------------------------------------------------------------------
# Projection — ensure recurring commitments have instances for every period
# ---------------------------------------------------------------------------

async def project_recurring_commitments(
    db: AsyncSession, user_id: uuid.UUID, through_date: date,
) -> int:
    """
    For each recurring commitment series, generate concrete instances
    up through `through_date` so every future period has rows to query.
    Returns how many new rows were created.
    """
    stmt = (
        select(Commitment)
        .where(
            Commitment.user_id == user_id,
            Commitment.is_recurring.is_(True),
            Commitment.is_active.is_(True),
        )
        .order_by(Commitment.due_date.desc())
    )
    all_recurring = list((await db.execute(stmt)).scalars().all())
    if not all_recurring:
        return 0

    # Group by series: (title, amount, direction, category, recurrence)
    # Keep the latest due_date as the projection anchor
    series: dict[tuple, Commitment] = {}
    existing_dates: dict[tuple, set[date]] = {}
    for c in all_recurring:
        key = (c.title, str(c.amount), c.direction, str(c.category_id or ""), c.recurrence)
        if key not in series:
            series[key] = c
        existing_dates.setdefault(key, set()).add(c.due_date)

    created = 0
    for key, latest in series.items():
        known = existing_dates[key]
        d = latest.due_date
        for _ in range(52):  # safety cap — max ~1 year of weekly
            d = _next_due_date(d, latest.recurrence)
            if d > through_date:
                break
            if d in known:
                continue
            db.add(Commitment(
                user_id=user_id,
                category_id=latest.category_id,
                title=latest.title,
                amount=latest.amount,
                direction=latest.direction,
                due_date=d,
                is_recurring=True,
                recurrence=latest.recurrence,
                confidence=latest.confidence,
                notes=latest.notes,
            ))
            known.add(d)
            created += 1

    if created:
        await db.flush()
    return created


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def get_commitments_for_period(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
    direction: CommitmentDirection | None = None,
    include_cleared: bool = False,
) -> list[Commitment]:
    """Active commitments whose due_date falls within [start, end)."""
    conditions = [
        Commitment.user_id == user_id,
        Commitment.is_active.is_(True),
        Commitment.due_date >= start,
        Commitment.due_date < end,
    ]
    if not include_cleared:
        conditions.append(Commitment.cleared_at.is_(None))
    if direction:
        conditions.append(Commitment.direction == direction)

    stmt = (
        select(Commitment)
        .options(selectinload(Commitment.category))
        .where(*conditions)
        .order_by(Commitment.due_date)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_commitment(
    db: AsyncSession, commitment_id: uuid.UUID, user_id: uuid.UUID,
) -> Commitment | None:
    c = await db.get(Commitment, commitment_id)
    if c and c.user_id == user_id:
        return c
    return None


async def create_commitment(
    db: AsyncSession, user_id: uuid.UUID,
    title: str, amount: Decimal, due_date: date,
    direction: str = "outflow",
    category_id: uuid.UUID | None = None,
    confidence: str = "confirmed",
    is_recurring: bool = False,
    recurrence: str | None = None,
    notes: str | None = None,
) -> Commitment:
    c = Commitment(
        user_id=user_id,
        category_id=category_id,
        title=title,
        amount=amount,
        direction=CommitmentDirection(direction),
        due_date=due_date,
        confidence=CommitmentConfidence(confidence),
        is_recurring=is_recurring,
        recurrence=CommitmentRecurrence(recurrence) if recurrence else None,
        notes=notes,
    )
    db.add(c)
    await db.flush()
    return c


async def update_commitment(
    db: AsyncSession, commitment_id: uuid.UUID, user_id: uuid.UUID,
    **kwargs,
) -> Commitment | None:
    c = await db.get(Commitment, commitment_id)
    if not c or c.user_id != user_id:
        return None
    for k, v in kwargs.items():
        if hasattr(c, k):
            setattr(c, k, v)
    await db.flush()
    return c


async def clear_commitment(
    db: AsyncSession, commitment_id: uuid.UUID, user_id: uuid.UUID,
) -> Commitment | None:
    """Mark a commitment as cleared (actual transaction matched)."""
    c = await db.get(Commitment, commitment_id)
    if not c or c.user_id != user_id:
        return None
    c.cleared_at = datetime.now(timezone.utc)

    if c.is_recurring and c.recurrence:
        _spawn_next_recurrence(db, c)

    await db.flush()
    return c


def _spawn_next_recurrence(db: AsyncSession, c: Commitment) -> None:
    """Create the next occurrence of a recurring commitment."""
    next_date = _next_due_date(c.due_date, c.recurrence)
    if next_date == c.due_date:
        return
    new = Commitment(
        user_id=c.user_id,
        category_id=c.category_id,
        title=c.title,
        amount=c.amount,
        direction=c.direction,
        due_date=next_date,
        is_recurring=True,
        recurrence=c.recurrence,
        confidence=c.confidence,
        notes=c.notes,
    )
    db.add(new)


async def delete_commitment(
    db: AsyncSession, commitment_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    c = await db.get(Commitment, commitment_id)
    if not c or c.user_id != user_id:
        return False
    await db.delete(c)
    await db.flush()
    return True


async def commitment_totals_for_period(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
) -> dict:
    """Aggregate uncleared commitment amounts by direction for a period."""
    stmt = (
        select(
            Commitment.direction,
            sa_func.sum(Commitment.amount).label("total"),
        )
        .where(
            Commitment.user_id == user_id,
            Commitment.is_active.is_(True),
            Commitment.cleared_at.is_(None),
            Commitment.due_date >= start,
            Commitment.due_date < end,
        )
        .group_by(Commitment.direction)
    )
    result = await db.execute(stmt)
    totals = {CommitmentDirection.OUTFLOW: Decimal("0.00"), CommitmentDirection.INFLOW: Decimal("0.00")}
    for row in result.all():
        totals[row.direction] = row.total or Decimal("0.00")
    return {
        "committed_out": float(totals[CommitmentDirection.OUTFLOW]),
        "committed_in": float(totals[CommitmentDirection.INFLOW]),
    }


async def commitments_by_category(
    db: AsyncSession, user_id: uuid.UUID,
    start: date, end: date,
) -> dict[str, float]:
    """Sum of uncleared outflow commitments grouped by category_id."""
    stmt = (
        select(
            Commitment.category_id,
            sa_func.sum(Commitment.amount).label("total"),
        )
        .where(
            Commitment.user_id == user_id,
            Commitment.is_active.is_(True),
            Commitment.cleared_at.is_(None),
            Commitment.due_date >= start,
            Commitment.due_date < end,
            Commitment.direction == CommitmentDirection.OUTFLOW,
            Commitment.category_id.isnot(None),
        )
        .group_by(Commitment.category_id)
    )
    result = await db.execute(stmt)
    return {str(r.category_id): float(r.total) for r in result.all()}
