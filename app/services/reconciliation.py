import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.reconciliation import Reconciliation, ReconciliationStatus
from app.models.transaction import Transaction


async def get_cleared_balance(db: AsyncSession, account_id: uuid.UUID) -> Decimal:
    acct = await db.get(Account, account_id)
    if not acct:
        return Decimal("0.00")
    result = await db.execute(
        select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
        .where(Transaction.account_id == account_id, Transaction.is_cleared.is_(True))
    )
    return acct.initial_balance + result.scalar()


async def get_uncleared_transactions(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    up_to_date: date,
) -> list[Transaction]:
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.is_cleared.is_(False),
            Transaction.date <= up_to_date,
        )
        .order_by(Transaction.date, Transaction.created_at)
    )
    return list(result.scalars().all())


async def save_draft(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    statement_date: date,
    statement_balance: Decimal,
    cleared_tx_ids: list[uuid.UUID],
) -> Reconciliation:
    draft = await get_draft_for_account(db, account_id)

    ids_json = json.dumps([str(tid) for tid in cleared_tx_ids])
    cleared_bal = await get_cleared_balance(db, account_id)

    if draft:
        draft.statement_date = statement_date
        draft.statement_balance = statement_balance
        draft.cleared_balance = cleared_bal
        draft.draft_cleared_ids = ids_json
    else:
        draft = Reconciliation(
            user_id=user_id,
            account_id=account_id,
            statement_date=statement_date,
            statement_balance=statement_balance,
            cleared_balance=cleared_bal,
            status=ReconciliationStatus.IN_PROGRESS,
            draft_cleared_ids=ids_json,
        )
        db.add(draft)

    await db.flush()
    return draft


async def finish_reconciliation(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    statement_date: date,
    statement_balance: Decimal,
    cleared_tx_ids: list[uuid.UUID],
) -> Reconciliation:
    for tx_id in cleared_tx_ids:
        tx = await db.get(Transaction, tx_id)
        if tx and tx.user_id == user_id and tx.account_id == account_id:
            tx.is_cleared = True

    await db.flush()

    cleared_bal = await get_cleared_balance(db, account_id)

    draft = await get_draft_for_account(db, account_id)
    if draft:
        draft.statement_date = statement_date
        draft.statement_balance = statement_balance
        draft.cleared_balance = cleared_bal
        draft.status = ReconciliationStatus.COMPLETED
        draft.draft_cleared_ids = None
        draft.completed_at = datetime.now(timezone.utc)
        rec = draft
    else:
        rec = Reconciliation(
            user_id=user_id,
            account_id=account_id,
            statement_date=statement_date,
            statement_balance=statement_balance,
            cleared_balance=cleared_bal,
            status=ReconciliationStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(rec)

    await db.flush()
    return rec


async def discard_draft(
    db: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    draft = await get_draft_for_account(db, account_id)
    if not draft or draft.user_id != user_id:
        return False
    await db.delete(draft)
    await db.flush()
    return True


async def get_draft_for_account(
    db: AsyncSession, account_id: uuid.UUID,
) -> Reconciliation | None:
    result = await db.execute(
        select(Reconciliation)
        .where(
            Reconciliation.account_id == account_id,
            Reconciliation.status == ReconciliationStatus.IN_PROGRESS,
        )
        .order_by(Reconciliation.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def parse_draft_ids(draft: Reconciliation) -> list[str]:
    if not draft.draft_cleared_ids:
        return []
    try:
        return json.loads(draft.draft_cleared_ids)
    except (json.JSONDecodeError, TypeError):
        return []


async def get_last_reconciliation(
    db: AsyncSession, account_id: uuid.UUID,
) -> Reconciliation | None:
    result = await db.execute(
        select(Reconciliation)
        .where(
            Reconciliation.account_id == account_id,
            Reconciliation.status == ReconciliationStatus.COMPLETED,
        )
        .order_by(Reconciliation.completed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_reconciliation_history(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
) -> list[Reconciliation]:
    result = await db.execute(
        select(Reconciliation)
        .where(
            Reconciliation.user_id == user_id,
            Reconciliation.account_id == account_id,
            Reconciliation.status == ReconciliationStatus.COMPLETED,
        )
        .order_by(Reconciliation.completed_at.desc())
    )
    return list(result.scalars().all())
