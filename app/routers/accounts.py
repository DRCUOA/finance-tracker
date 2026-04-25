import uuid
from datetime import date as date_cls, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import (
    AccountTerm,
    AccountType,
    CompoundingFrequency,
    CompoundingType,
)
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import interest as interest_svc
from app.services import reconciliation as recon_svc
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

    acct_recon = {}
    for acct in accounts:
        last = await recon_svc.get_last_reconciliation(db, acct.id)
        draft = await recon_svc.get_draft_for_account(db, acct.id)
        cleared_bal = await recon_svc.get_cleared_balance(db, acct.id)
        acct_recon[str(acct.id)] = {
            "last": last, "draft": draft, "cleared_balance": cleared_bal,
        }

    return templates.TemplateResponse(request, "accounts/list.html", {
        "user": user,
        "assets": assets, "liabilities": liabilities,
        "total_assets": total_assets, "total_liabilities": total_liabilities,
        "net_worth": total_assets - total_liabilities,
        "account_types": AccountType,
        "coverage": coverage,
        "acct_recon": acct_recon,
    })


@router.get("/create", response_class=HTMLResponse)
async def create_form(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "accounts/form.html", {
        "user": user,
        "account": None,
        "account_types": AccountType,
        "account_terms": AccountTerm,
        "compounding_types": CompoundingType,
        "compounding_frequencies": CompoundingFrequency,
        "retro_result": None,
        "retro_detail": None,
    })


def _parse_interest_rate(raw: str) -> Decimal | None:
    """Empty/blank → accrual disabled; otherwise best-effort Decimal parse."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_opened_on(raw: str) -> date_cls:
    """Parse the user-supplied open date (HTML5 ``<input type=date>`` ISO
    format). Empty/invalid → today, matching the column's server default so
    the form never blocks save on a blank field.
    """
    if raw is None or str(raw).strip() == "":
        return datetime.utcnow().date()
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return datetime.utcnow().date()


@router.post("/create")
async def create_account(
    request: Request,
    name: str = Form(...),
    account_type: str = Form(...),
    term: str = Form("short"),
    currency: str = Form("USD"),
    initial_balance: str = Form("0.00"),
    institution: str = Form(""),
    opened_on: str = Form(""),
    is_cashflow: bool = Form(True),
    interest_rate: str = Form(""),
    compounding_type: str = Form("compound"),
    compounding_frequency: str = Form("monthly"),
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
        is_cashflow=is_cashflow,
        opened_on=_parse_opened_on(opened_on),
        interest_rate=_parse_interest_rate(interest_rate),
        compounding_type=CompoundingType(compounding_type),
        compounding_frequency=CompoundingFrequency(compounding_frequency),
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
    # Surface retro re-evaluation result (post-redirect-get pattern via
    # query params). Keys: retro=ok|nochange|error, delta=<decimal>,
    # code=<no_rate|...>. Template reads retro_result and renders a banner.
    retro = request.query_params.get("retro")
    retro_result = None
    if retro == "ok":
        retro_result = {
            "kind": "ok",
            "delta": request.query_params.get("delta", ""),
        }
    elif retro == "nochange":
        retro_result = {"kind": "nochange"}
    elif retro == "error":
        retro_result = {
            "kind": "error",
            "code": request.query_params.get("code", ""),
        }
    return templates.TemplateResponse(request, "accounts/form.html", {
        "user": user,
        "account": account,
        "account_types": AccountType,
        "account_terms": AccountTerm,
        "compounding_types": CompoundingType,
        "compounding_frequencies": CompoundingFrequency,
        "retro_result": retro_result,
        "retro_detail": None,
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
    opened_on: str = Form(""),
    is_cashflow: bool = Form(False),
    is_active: bool = Form(False),
    interest_rate: str = Form(""),
    compounding_type: str = Form("compound"),
    compounding_frequency: str = Form("monthly"),
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
        institution=institution or None,
        opened_on=_parse_opened_on(opened_on),
        is_cashflow=is_cashflow, is_active=is_active,
        interest_rate=_parse_interest_rate(interest_rate),
        compounding_type=CompoundingType(compounding_type),
        compounding_frequency=CompoundingFrequency(compounding_frequency),
    )
    await acct_svc.recalculate_balance(db, account_id)
    return RedirectResponse(url="/accounts", status_code=302)


@router.post("/{account_id}/reevaluate-interest", response_class=HTMLResponse)
async def reevaluate_interest(
    account_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Post a retro interest true-up and render the edit form with the
    full calculation breakdown in a modal.

    Returns the edit-form template directly (rather than POST/redirect/GET)
    so we can pass a populated :class:`RetroResult` into context for the
    modal — that data is too rich to round-trip via query params, and we
    avoid an in-memory cache. Refreshing this page would re-submit the POST,
    which simply re-evaluates and produces a no-op (delta = 0) modal —
    accept that minor wart over carrying state across processes.

    The unconfigured-rate case still uses redirect → banner since there's
    nothing to model in a modal.
    """
    account = await acct_svc.get_account(db, account_id, user.id)
    if not account:
        return RedirectResponse(url="/accounts", status_code=302)
    try:
        result = await interest_svc.retro_reevaluate_interest(db, account)
    except interest_svc.InterestNotConfiguredError:
        return RedirectResponse(
            url=f"/accounts/{account_id}/edit?retro=error&code=no_rate",
            status_code=302,
        )
    return templates.TemplateResponse(request, "accounts/form.html", {
        "user": user,
        "account": account,
        "account_types": AccountType,
        "account_terms": AccountTerm,
        "compounding_types": CompoundingType,
        "compounding_frequencies": CompoundingFrequency,
        # No banner — the modal carries everything for the success path.
        "retro_result": None,
        "retro_detail": result,
    })


@router.post("/{account_id}/delete")
async def delete_account(
    account_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await acct_svc.delete_account(db, account_id, user.id)
    return RedirectResponse(url="/accounts", status_code=302)
