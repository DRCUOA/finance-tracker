import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import reconciliation as recon_svc
from app.templating import templates

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get("", response_class=HTMLResponse)
async def reconciliation_index(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id)
    account_info = []
    for acct in accounts:
        last = await recon_svc.get_last_reconciliation(db, acct.id)
        draft = await recon_svc.get_draft_for_account(db, acct.id)
        cleared_bal = await recon_svc.get_cleared_balance(db, acct.id)
        account_info.append({
            "account": acct,
            "last_reconciliation": last,
            "draft": draft,
            "cleared_balance": cleared_bal,
        })
    return templates.TemplateResponse(request, "reconciliation/index.html", {
        "user": user,
        "account_info": account_info,
    })


@router.get("/{account_id}", response_class=HTMLResponse)
async def reconcile_account(
    account_id: uuid.UUID,
    request: Request,
    statement_date: str = Query(...),
    statement_balance: str = Query(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    acct = await acct_svc.get_account(db, account_id, user.id)
    if not acct:
        return RedirectResponse(url="/reconciliation", status_code=302)

    try:
        s_date = date.fromisoformat(statement_date)
        s_balance = Decimal(statement_balance)
    except (ValueError, InvalidOperation):
        return RedirectResponse(url="/reconciliation", status_code=302)

    cleared_bal = await recon_svc.get_cleared_balance(db, account_id)
    uncleared = await recon_svc.get_uncleared_transactions(db, user.id, account_id, s_date)

    draft = await recon_svc.get_draft_for_account(db, account_id)
    draft_ids = recon_svc.parse_draft_ids(draft) if draft else []

    return templates.TemplateResponse(request, "reconciliation/reconcile.html", {
        "user": user,
        "account": acct,
        "statement_date": statement_date,
        "statement_balance": str(s_balance),
        "cleared_balance": str(cleared_bal),
        "transactions": uncleared,
        "draft_ids": draft_ids,
    })


@router.post("/{account_id}/save-draft")
async def save_draft(
    account_id: uuid.UUID,
    request: Request,
    statement_date: str = Form(...),
    statement_balance: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    cleared_ids = [uuid.UUID(tid) for tid in form.getlist("cleared_ids")]

    try:
        s_date = date.fromisoformat(statement_date)
        s_balance = Decimal(statement_balance)
    except (ValueError, InvalidOperation):
        return RedirectResponse(url="/reconciliation", status_code=302)

    await recon_svc.save_draft(
        db, user.id, account_id, s_date, s_balance, cleared_ids,
    )
    return RedirectResponse(url="/reconciliation", status_code=302)


@router.post("/{account_id}/finish")
async def finish_reconciliation(
    account_id: uuid.UUID,
    request: Request,
    statement_date: str = Form(...),
    statement_balance: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    cleared_ids = [uuid.UUID(tid) for tid in form.getlist("cleared_ids")]

    try:
        s_date = date.fromisoformat(statement_date)
        s_balance = Decimal(statement_balance)
    except (ValueError, InvalidOperation):
        return RedirectResponse(url="/reconciliation", status_code=302)

    await recon_svc.finish_reconciliation(
        db, user.id, account_id, s_date, s_balance, cleared_ids,
    )
    await acct_svc.recalculate_balance(db, account_id)
    return RedirectResponse(url="/reconciliation", status_code=302)


@router.post("/{account_id}/discard-draft")
async def discard_draft(
    account_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await recon_svc.discard_draft(db, account_id, user.id)
    return RedirectResponse(url="/reconciliation", status_code=302)


@router.get("/{account_id}/history", response_class=HTMLResponse)
async def reconciliation_history(
    account_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    acct = await acct_svc.get_account(db, account_id, user.id)
    if not acct:
        return RedirectResponse(url="/reconciliation", status_code=302)
    history = await recon_svc.get_reconciliation_history(db, user.id, account_id)
    return templates.TemplateResponse(request, "reconciliation/history.html", {
        "user": user,
        "account": acct,
        "history": history,
    })
