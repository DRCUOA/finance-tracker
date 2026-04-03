import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
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

    summary = await report_svc.period_summary(db, user.id, start, end)
    budget_data = await report_svc.budget_vs_actual(
        db, user.id, start, end, period=period,
    )
    averages, avg_label = await report_svc.category_averages(
        db, user.id, periods=6, period=period,
    )
    net_history = await report_svc.net_balance_history(
        db, user.id, steps=12, period=period,
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

    transactions = await report_svc.spending_breakdown(
        db, user.id, category_id, start, end,
    )
    return templates.TemplateResponse(request, "reports/breakdown.html", {
        "user": user,
        "transactions": transactions,
        "period": period,
        "period_label": label,
        "ref": ref_date.isoformat(),
    })
