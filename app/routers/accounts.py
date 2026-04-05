import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import AccountTerm, AccountType
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import reports as report_svc
from app.templating import templates

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_class=HTMLResponse)
async def list_accounts(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id, active_only=False)
    assets = [a for a in accounts if a.group.value == "asset"]
    liabilities = [a for a in accounts if a.group.value == "liability"]
    total_assets = sum(a.current_balance for a in assets)
    total_liabilities = sum(abs(a.current_balance) for a in liabilities)
    coverage = await report_svc.import_coverage(db, user.id)
    return templates.TemplateResponse(request, "accounts/list.html", {
        "user": user,
        "assets": assets, "liabilities": liabilities,
        "total_assets": total_assets, "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
        "account_types": AccountType,
        "coverage": coverage,
    })


@router.get("/create", response_class=HTMLResponse)
async def create_form(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "accounts/form.html", {
        "user": user,
        "account": None, "account_types": AccountType, "account_terms": AccountTerm,
    })


@router.post("/create")
async def create_account(
    request: Request,
    name: str = Form(...),
    account_type: str = Form(...),
    term: str = Form("short"),
    currency: str = Form("USD"),
    initial_balance: str = Form("0.00"),
    institution: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        bal = Decimal(initial_balance)
    except InvalidOperation:
        bal = Decimal("0.00")
    await acct_svc.create_account(
        db, user.id, name, AccountType(account_type),
        currency, bal, institution or None,
        term=AccountTerm(term),
    )
    return RedirectResponse(url="/accounts", status_code=302)


@router.get("/{account_id}/edit", response_class=HTMLResponse)
async def edit_form(
    account_id: uuid.UUID, request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    account = await acct_svc.get_account(db, account_id, user.id)
    if not account:
        return RedirectResponse(url="/accounts", status_code=302)
    return templates.TemplateResponse(request, "accounts/form.html", {
        "user": user,
        "account": account, "account_types": AccountType, "account_terms": AccountTerm,
    })


@router.post("/{account_id}/edit")
async def update_account(
    account_id: uuid.UUID, request: Request,
    name: str = Form(...),
    account_type: str = Form(...),
    term: str = Form("short"),
    currency: str = Form("USD"),
    initial_balance: str = Form("0.00"),
    institution: str = Form(""),
    is_active: bool = Form(False),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        bal = Decimal(initial_balance)
    except InvalidOperation:
        bal = Decimal("0.00")
    await acct_svc.update_account(
        db, account_id, user.id,
        name=name, account_type=AccountType(account_type),
        term=AccountTerm(term),
        currency=currency, initial_balance=bal,
        institution=institution or None, is_active=is_active,
    )
    await acct_svc.recalculate_balance(db, account_id)
    return RedirectResponse(url="/accounts", status_code=302)


@router.post("/{account_id}/delete")
async def delete_account(
    account_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await acct_svc.delete_account(db, account_id, user.id)
    return RedirectResponse(url="/accounts", status_code=302)
