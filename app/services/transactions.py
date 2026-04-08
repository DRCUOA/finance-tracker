import json
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, func as sa_func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.transaction import Transaction
from app.models.account import Account
from app.models.category import Category
from app.models.reconciliation import Reconciliation, ReconciliationStatus


class DuplicateTransactionError(Exception):
    """Raised when a transaction to be created matches an existing one."""


async def get_transactions(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    account_ids: list[uuid.UUID] | None = None,
    category_id: uuid.UUID | None = None,
    uncategorized: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    is_cleared: bool | None = None,
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
    elif account_ids is not None:
        stmt = stmt.where(Transaction.account_id.in_(account_ids))
    if category_id:
        stmt = stmt.where(Transaction.category_id == category_id)
    elif uncategorized:
        stmt = stmt.where(Transaction.category_id.is_(None))
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
    if is_cleared is not None:
        stmt = stmt.where(Transaction.is_cleared == is_cleared)

    count_stmt = select(sa_func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar()

    if sort_by == "account":
        stmt = stmt.outerjoin(Account, Transaction.account_id == Account.id)
        sort_col = Account.name
    elif sort_by == "category":
        stmt = stmt.outerjoin(Category, Transaction.category_id == Category.id)
        sort_col = Category.name
    else:
        sort_col = getattr(Transaction, sort_by, Transaction.date)
    if sort_dir == "asc":
        stmt = stmt.order_by(sort_col.asc(), Transaction.created_at.asc())
    else:
        stmt = stmt.order_by(sort_col.desc(), Transaction.created_at.desc())

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def check_duplicate(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    dt: date, amount: Decimal, description: str,
    reference: str | None = None,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """Return True if a matching transaction already exists."""
    if reference:
        stmt = select(Transaction.id).where(
            and_(
                Transaction.account_id == account_id,
                Transaction.reference == reference,
            )
        )
        if exclude_id:
            stmt = stmt.where(Transaction.id != exclude_id)
        stmt = stmt.limit(1)
        result = await db.execute(stmt)
        if result.first():
            return True

    stmt = select(Transaction.id).where(
        and_(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.date == dt,
            Transaction.amount == amount,
            sa_func.lower(sa_func.trim(Transaction.description))
            == description.lower().strip(),
        )
    )
    if exclude_id:
        stmt = stmt.where(Transaction.id != exclude_id)
    stmt = stmt.limit(1)
    result = await db.execute(stmt)
    return result.first() is not None


async def create_transaction(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    dt: date, amount: Decimal, description: str,
    category_id: uuid.UUID | None = None,
    reference: str | None = None, notes: str | None = None,
    force: bool = False,
) -> Transaction:
    if not force and await check_duplicate(
        db, user_id, account_id, dt, amount, description, reference,
    ):
        raise DuplicateTransactionError(
            f"A transaction matching '{description}' on {dt} for {amount} already exists."
        )

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


async def get_filtered_transaction_ids(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    uncategorized: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
) -> list[str]:
    stmt = select(Transaction.id).where(Transaction.user_id == user_id)
    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    if category_id:
        stmt = stmt.where(Transaction.category_id == category_id)
    elif uncategorized:
        stmt = stmt.where(Transaction.category_id.is_(None))
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
    result = await db.execute(stmt)
    return [str(row[0]) for row in result.all()]


async def get_locked_tx_ids(db: AsyncSession, tx_ids: list[uuid.UUID]) -> set[str]:
    """Return IDs of transactions locked by a completed or in-progress reconciliation."""
    if not tx_ids:
        return set()
    locked: set[str] = set()
    result = await db.execute(
        select(Transaction.id).where(
            Transaction.id.in_(tx_ids),
            Transaction.is_cleared.is_(True),
        )
    )
    locked.update(str(r[0]) for r in result.all())

    draft_result = await db.execute(
        select(Reconciliation.draft_cleared_ids).where(
            Reconciliation.status == ReconciliationStatus.IN_PROGRESS,
            Reconciliation.draft_cleared_ids.isnot(None),
        )
    )
    str_ids = {str(tid) for tid in tx_ids}
    for row in draft_result.all():
        try:
            draft_ids = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        locked.update(did for did in draft_ids if did in str_ids)
    return locked


async def is_tx_locked(db: AsyncSession, tx_id: uuid.UUID) -> bool:
    return bool(await get_locked_tx_ids(db, [tx_id]))


async def get_tx_detail(db: AsyncSession, tx_id: uuid.UUID, user_id: uuid.UUID) -> dict | None:
    """Full transaction detail with lock status for edit modals."""
    result = await db.execute(
        select(Transaction)
        .options(selectinload(Transaction.account), selectinload(Transaction.category))
        .where(Transaction.id == tx_id, Transaction.user_id == user_id)
    )
    tx = result.scalar_one_or_none()
    if not tx:
        return None
    locked = await is_tx_locked(db, tx_id)
    return {
        "id": str(tx.id),
        "date": tx.date.isoformat(),
        "description": tx.description,
        "amount": float(tx.amount),
        "account_id": str(tx.account_id),
        "account_name": tx.account.name if tx.account else "",
        "category_id": str(tx.category_id) if tx.category_id else "",
        "category_name": tx.category.name if tx.category else "",
        "reference": tx.reference or "",
        "notes": tx.notes or "",
        "is_cleared": tx.is_cleared,
        "is_locked": locked,
    }


async def batch_delete(db: AsyncSession, tx_ids: list[uuid.UUID], user_id: uuid.UUID) -> int:
    count = 0
    for tid in tx_ids:
        tx = await db.get(Transaction, tid)
        if tx and tx.user_id == user_id:
            await db.delete(tx)
            count += 1
    await db.flush()
    return count
