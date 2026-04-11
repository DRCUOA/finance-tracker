import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import reports as report_svc
from app.templating import templates

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    period: str = Query("month"),
    ref: str = Query(""),
    year: int = Query(0),
    month: int = Query(0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period not in ("week", "month"):
        period = "month"

    today = date.today()

    if ref:
        ref_date = date.fromisoformat(ref)
    elif year and month:
        ref_date = date(year, month, 1)
    else:
        ref_date = today

    start, end = report_svc.period_bounds(ref_date, period)
    label = report_svc.period_label(ref_date, period)

    accounts = await acct_svc.get_accounts(db, user.id)
    has_non_cashflow = any(not a.is_cashflow for a in accounts)
    cashflow_ids = [a.id for a in accounts if a.is_cashflow] if has_non_cashflow else None

    summary = await report_svc.period_summary(db, user.id, start, end, account_ids=cashflow_ids)
    budget_data = await report_svc.budget_vs_actual(
        db, user.id, start, end, period=period, account_ids=cashflow_ids,
    )
    averages, avg_label = await report_svc.category_averages(
        db, user.id, periods=6, period=period, account_ids=cashflow_ids,
    )
    net_history = await report_svc.net_balance_history(
        db, user.id, steps=12, period=period,
    )

    income_trend = await report_svc.income_vs_spending_trend(
        db, user.id, periods=6, period=period, account_ids=cashflow_ids,
    )
    category_comparison = await report_svc.spending_by_category_comparison(
        db, user.id, start, end, period=period, account_ids=cashflow_ids,
    )
    fixed_flexible = await report_svc.fixed_vs_flexible_summary(
        db, user.id, start, end, account_ids=cashflow_ids,
    )
    cashflow = await report_svc.cashflow_trend(
        db, user.id, periods=12, account_ids=cashflow_ids,
    )

    prev_ref = report_svc.step_period(ref_date, -1, period)
    next_ref = report_svc.step_period(ref_date, 1, period)
    prev_url = f"/reports?period={period}&ref={prev_ref.isoformat()}"
    next_url = f"/reports?period={period}&ref={next_ref.isoformat()}"
    base_url = f"/reports?ref={ref_date.isoformat()}"

    return templates.TemplateResponse(request, "reports/index.html", {
        "user": user,
        "period": period,
        "period_label": label,
        "start": start,
        "end": end,
        "summary": summary,
        "budget_data": budget_data,
        "averages": averages,
        "avg_label": avg_label,
        "net_history": net_history,
        "income_trend": income_trend,
        "category_comparison": category_comparison,
        "fixed_flexible": fixed_flexible,
        "cashflow": cashflow,
        "prev_url": prev_url,
        "next_url": next_url,
        "base_url": base_url,
    })


@router.get("/breakdown/{category_id}", response_class=HTMLResponse)
async def spending_breakdown(
    category_id: uuid.UUID,
    request: Request,
    period: str = Query("month"),
    ref: str = Query(""),
    year: int = Query(0),
    month: int = Query(0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    today = date.today()

    if ref:
        ref_date = date.fromisoformat(ref)
    elif year and month:
        ref_date = date(year, month, 1)
    else:
        ref_date = today

    if period not in ("week", "month"):
        period = "month"

    start, end = report_svc.period_bounds(ref_date, period)
    label = report_svc.period_label(ref_date, period)

    breakdown_accounts = await acct_svc.get_accounts(db, user.id)
    has_nc = any(not a.is_cashflow for a in breakdown_accounts)
    cf_ids = [a.id for a in breakdown_accounts if a.is_cashflow] if has_nc else None

    transactions = await report_svc.spending_breakdown(
        db, user.id, category_id, start, end, account_ids=cf_ids,
    )
    return templates.TemplateResponse(request, "reports/breakdown.html", {
        "user": user,
        "transactions": transactions,
        "period": period,
        "period_label": label,
        "ref": ref_date.isoformat(),
    })


@router.get("/deepdive", response_class=HTMLResponse)
async def category_deepdive_page(
    request: Request,
    period: str = Query("month"),
    ref: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period not in ("week", "month"):
        period = "month"

    today = date.today()
    ref_date = date.fromisoformat(ref) if ref else today

    start, end = report_svc.period_bounds(ref_date, period)
    label = report_svc.period_label(ref_date, period)

    accounts = await acct_svc.get_accounts(db, user.id)
    has_non_cashflow = any(not a.is_cashflow for a in accounts)
    cashflow_ids = [a.id for a in accounts if a.is_cashflow] if has_non_cashflow else None

    summary = await report_svc.period_summary(db, user.id, start, end, account_ids=cashflow_ids)

    prev_ref = report_svc.step_period(ref_date, -1, period)
    next_ref = report_svc.step_period(ref_date, 1, period)
    prev_url = f"/reports/deepdive?period={period}&ref={prev_ref.isoformat()}"
    next_url = f"/reports/deepdive?period={period}&ref={next_ref.isoformat()}"
    base_url = f"/reports/deepdive?ref={ref_date.isoformat()}"

    return templates.TemplateResponse(request, "reports/deepdive.html", {
        "user": user,
        "period": period,
        "period_label": label,
        "start": start,
        "end": end,
        "summary": summary,
        "prev_url": prev_url,
        "next_url": next_url,
        "base_url": base_url,
    })


@router.get("/deepdive/category/{category_id}")
async def category_deepdive_detail(
    category_id: uuid.UUID,
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id)
    has_nc = any(not a.is_cashflow for a in accounts)
    cf_ids = [a.id for a in accounts if a.is_cashflow] if has_nc else None

    d_from = date.fromisoformat(date_from) if date_from else None
    d_to = date.fromisoformat(date_to) if date_to else None

    data = await report_svc.category_transactions_detail(
        db, user.id, category_id,
        date_from=d_from, date_to=d_to, account_ids=cf_ids,
    )
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return data
