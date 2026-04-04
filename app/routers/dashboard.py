from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import AccountGroup, AccountTerm
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import reports as report_svc
from app.services import transactions as tx_svc
from app.templating import templates

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

VALID_TERMS = {t.value for t in AccountTerm}


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    period: str = Query("month"),
    ref: str = Query(""),
    term: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period not in ("week", "month"):
        period = "month"

    active_term: AccountTerm | None = None
    if term in VALID_TERMS:
        active_term = AccountTerm(term)

    today = date.today()
    ref_date = date.fromisoformat(ref) if ref else today

    start, end = report_svc.period_bounds(ref_date, period)
    label = report_svc.period_label(ref_date, period)

    accounts = await acct_svc.get_accounts(db, user.id, term=active_term)
    acct_ids = [a.id for a in accounts] if active_term else None

    total_assets = sum(a.current_balance for a in accounts if a.group == AccountGroup.ASSET)
    total_liabilities = sum(abs(a.current_balance) for a in accounts if a.group == AccountGroup.LIABILITY)
    net_worth = total_assets - total_liabilities

    summary = await report_svc.period_summary(db, user.id, start, end, account_ids=acct_ids)
    budget_data = await report_svc.budget_vs_actual(
        db, user.id, start, end, period=period, account_ids=acct_ids,
    )
    net_history = await report_svc.net_balance_history(
        db, user.id, steps=12, period=period, ref_date=ref_date,
        account_ids=acct_ids,
    )

    recent_txs, _ = await tx_svc.get_transactions(
        db, user.id, account_ids=acct_ids, page=1, per_page=10,
    )
    coverage = await report_svc.import_coverage(db, user.id)

    budget_total = sum(b["budgeted"] for b in budget_data)
    budget_spent = sum(b["actual"] for b in budget_data)

    term_qs = f"&term={active_term.value}" if active_term else ""
    prev_ref = report_svc.step_period(ref_date, -1, period)
    next_ref = report_svc.step_period(ref_date, 1, period)
    prev_url = f"/dashboard?period={period}&ref={prev_ref.isoformat()}{term_qs}"
    next_url = f"/dashboard?period={period}&ref={next_ref.isoformat()}{term_qs}"
    base_url = f"/dashboard?ref={ref_date.isoformat()}{term_qs}"

    return templates.TemplateResponse(request, "dashboard/index.html", {
        "user": user,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "net_worth": net_worth,
        "summary": summary,
        "budget_total": budget_total,
        "budget_spent": budget_spent,
        "net_history": net_history,
        "recent_txs": recent_txs,
        "accounts": accounts,
        "period": period,
        "period_label": label,
        "prev_url": prev_url,
        "next_url": next_url,
        "base_url": base_url,
        "active_term": active_term.value if active_term else "",
        "term_base_url": f"/dashboard?period={period}&ref={ref_date.isoformat()}",
        "coverage": coverage,
    })
