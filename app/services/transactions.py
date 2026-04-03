import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, func as sa_func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.transaction import Transaction
from app.models.account import Account
from app.models.category import Category


async def get_transactions(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    is_reconciled: bool | None = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Transaction], int]:
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
    )

    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    if category_id:
        stmt = stmt.where(Transaction.category_id == category_id)
    if date_from:
        stmt = stmt.where(Transaction.date >= date_from)
    if date_to:
        stmt = stmt.where(Transaction.date <= date_to)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Transaction.description.ilike(pattern),
                Transaction.notes.ilike(pattern),
                Transaction.reference.ilike(pattern),
            )
        )
    if min_amount is not None:
        stmt = stmt.where(Transaction.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(Transaction.amount <= max_amount)
    if is_reconciled is not None:
        stmt = stmt.where(Transaction.is_reconciled == is_reconciled)

    count_stmt = select(sa_func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar()

    sort_col = getattr(Transaction, sort_by, Transaction.date)
    if sort_dir == "asc":
        stmt = stmt.order_by(sort_col.asc(), Transaction.created_at.asc())
    else:
        stmt = stmt.order_by(sort_col.desc(), Transaction.created_at.desc())

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def create_transaction(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    dt: date, amount: Decimal, description: str,
    category_id: uuid.UUID | None = None,
    reference: str | None = None, notes: str | None = None,
) -> Transaction:
    tx = Transaction(
        user_id=user_id, account_id=account_id, date=dt,
        amount=amount, description=description,
        original_description=description,
        category_id=category_id, reference=reference, notes=notes,
    )
    db.add(tx)
    await db.flush()
    return tx


async def get_transaction(db: AsyncSession, tx_id: uuid.UUID, user_id: uuid.UUID) -> Transaction | None:
    tx = await db.get(Transaction, tx_id)
    if not tx or tx.user_id != user_id:
        return None
    return tx


async def update_transaction(db: AsyncSession, tx_id: uuid.UUID, user_id: uuid.UUID, **kwargs) -> Transaction | None:
    tx = await db.get(Transaction, tx_id)
    if not tx or tx.user_id != user_id:
        return None
    for k, v in kwargs.items():
        if hasattr(tx, k) and k not in ("id", "user_id", "created_at"):
            setattr(tx, k, v)
    await db.flush()
    return tx


async def delete_transaction(db: AsyncSession, tx_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    tx = await db.get(Transaction, tx_id)
    if not tx or tx.user_id != user_id:
        return False
    await db.delete(tx)
    await db.flush()
    return True


async def batch_categorise(db: AsyncSession, tx_ids: list[uuid.UUID], user_id: uuid.UUID, category_id: uuid.UUID) -> int:
    count = 0
    for tid in tx_ids:
        tx = await db.get(Transaction, tid)
        if tx and tx.user_id == user_id:
            tx.category_id = category_id
            count += 1
    await db.flush()
    return count


async def batch_delete(db: AsyncSession, tx_ids: list[uuid.UUID], user_id: uuid.UUID) -> int:
    count = 0
    for tid in tx_ids:
        tx = await db.get(Transaction, tid)
        if tx and tx.user_id == user_id:
            await db.delete(tx)
            count += 1
    await db.flush()
    return count
