import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.statement import Statement, StatementLine, StatementStatus
from app.models.transaction import Transaction


async def get_statement(
    db: AsyncSession, statement_id: uuid.UUID, user_id: uuid.UUID,
) -> Statement | None:
    stmt = (
        select(Statement)
        .where(Statement.id == statement_id, Statement.user_id == user_id)
        .options(selectinload(Statement.account), selectinload(Statement.lines))
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_statement(
    db: AsyncSession, statement_id: uuid.UUID, user_id: uuid.UUID,
    *, filename: str | None = None, account_id: uuid.UUID | None = None,
) -> Statement | None:
    statement = await get_statement(db, statement_id, user_id)
    if not statement:
        return None

    if filename is not None:
        statement.filename = filename
    if account_id is not None:
        statement.account_id = account_id

    await db.flush()
    return await get_statement(db, statement_id, user_id)


async def delete_statement(
    db: AsyncSession, statement_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    statement = await get_statement(db, statement_id, user_id)
    if not statement:
        return False

    line_ids = [line.id for line in statement.lines]

    if line_ids:
        await db.execute(
            update(Transaction)
            .where(Transaction.statement_line_id.in_(line_ids))
            .values(statement_line_id=None, is_reconciled=False)
        )

    await db.delete(statement)
    await db.flush()
    return True
