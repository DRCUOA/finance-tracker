import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.statement import MatchType, Statement, StatementLine
from app.models.transaction import Transaction
from app.models.user import User
from app.routers.auth import require_user
from app.services import reconciler
from app.templating import templates

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get("", response_class=HTMLResponse)
async def reconciliation_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Statement)
        .where(Statement.user_id == user.id)
        .options(selectinload(Statement.account))
        .order_by(Statement.created_at.desc())
    )
    result = await db.execute(stmt)
    statements = result.scalars().all()
    return templates.TemplateResponse(request, "reconciliation/list.html", {
        "user": user, "statements": statements,
    })


@router.post("/{statement_id}/run")
async def run_reconciliation(
    statement_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await reconciler.auto_match_statement(db, user.id, statement_id)
    return RedirectResponse(url=f"/reconciliation/{statement_id}", status_code=302)


@router.get("/{statement_id}", response_class=HTMLResponse)
async def review_matches(
    statement_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    stmt_q = (
        select(Statement)
        .where(Statement.id == statement_id)
        .options(selectinload(Statement.account), selectinload(Statement.lines))
    )
    statement = (await db.execute(stmt_q)).scalar_one_or_none()
    if not statement or statement.user_id != user.id:
        return RedirectResponse(url="/reconciliation", status_code=302)

    lines = sorted(statement.lines, key=lambda l: l.date)

    matched_txs = {}
    for line in lines:
        if line.matched_transaction_id:
            tx = await db.get(Transaction, line.matched_transaction_id)
            if tx:
                matched_txs[str(line.id)] = tx

    unmatched_tx_q = select(Transaction).where(
        Transaction.user_id == user.id,
        Transaction.account_id == statement.account_id,
        Transaction.statement_line_id.is_(None),
    ).order_by(Transaction.date)
    unmatched_txs = (await db.execute(unmatched_tx_q)).scalars().all()

    exact = [l for l in lines if l.match_type == MatchType.EXACT]
    keyword = [l for l in lines if l.match_type == MatchType.KEYWORD]
    fuzzy = [l for l in lines if l.match_type == MatchType.FUZZY]
    manual = [l for l in lines if l.match_type == MatchType.MANUAL]
    unmatched = [l for l in lines if l.match_type == MatchType.NONE]

    return templates.TemplateResponse(request, "reconciliation/review.html", {
        "user": user, "statement": statement,
        "exact": exact, "keyword": keyword, "fuzzy": fuzzy,
        "manual": manual, "unmatched": unmatched,
        "matched_txs": matched_txs, "unmatched_txs": unmatched_txs,
    })


@router.post("/confirm/{line_id}")
async def confirm_match(
    line_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    line = await db.get(StatementLine, line_id)
    await reconciler.confirm_match(db, line_id, user.id)
    return RedirectResponse(url=f"/reconciliation/{line.statement_id}", status_code=302)


@router.post("/reject/{line_id}")
async def reject_match(
    line_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    line = await db.get(StatementLine, line_id)
    await reconciler.reject_match(db, line_id)
    return RedirectResponse(url=f"/reconciliation/{line.statement_id}", status_code=302)


@router.post("/manual/{line_id}")
async def manual_match(
    line_id: uuid.UUID,
    tx_id: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    line = await db.get(StatementLine, line_id)
    await reconciler.manual_match(db, line_id, uuid.UUID(tx_id), user.id)
    return RedirectResponse(url=f"/reconciliation/{line.statement_id}", status_code=302)
