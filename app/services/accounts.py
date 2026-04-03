import uuid
from decimal import Decimal

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.transaction import Transaction


async def get_accounts(db: AsyncSession, user_id: uuid.UUID, active_only: bool = True) -> list[Account]:
    stmt = select(Account).where(Account.user_id == user_id).order_by(Account.sort_order)
    if active_only:
        stmt = stmt.where(Account.is_active.is_(True))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_account(db: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID) -> Account | None:
    acct = await db.get(Account, account_id)
    if not acct or acct.user_id != user_id:
        return None
    return acct


async def create_account(
    db: AsyncSession, user_id: uuid.UUID, name: str,
    account_type: AccountType, currency: str = "USD",
    initial_balance: Decimal = Decimal("0.00"),
    institution: str | None = None,
) -> Account:
    max_order = await db.execute(
        select(sa_func.coalesce(sa_func.max(Account.sort_order), -1))
        .where(Account.user_id == user_id)
    )
    next_order = max_order.scalar() + 1
    acct = Account(
        user_id=user_id, name=name, account_type=account_type,
        currency=currency, initial_balance=initial_balance,
        current_balance=initial_balance, institution=institution,
        sort_order=next_order,
    )
    db.add(acct)
    await db.flush()
    return acct


async def update_account(db: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID, **kwargs) -> Account | None:
    acct = await db.get(Account, account_id)
    if not acct or acct.user_id != user_id:
        return None
    for k, v in kwargs.items():
        if hasattr(acct, k) and k not in ("id", "user_id", "created_at"):
            setattr(acct, k, v)
    await db.flush()
    return acct


async def delete_account(db: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    acct = await db.get(Account, account_id)
    if not acct or acct.user_id != user_id:
        return False
    await db.delete(acct)
    await db.flush()
    return True


async def recalculate_balance(db: AsyncSession, account_id: uuid.UUID) -> Decimal:
    acct = await db.get(Account, account_id)
    if not acct:
        return Decimal("0.00")
    result = await db.execute(
        select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
        .where(Transaction.account_id == account_id)
    )
    tx_sum = result.scalar()
    acct.current_balance = acct.initial_balance + tx_sum
    await db.flush()
    return acct.current_balance
