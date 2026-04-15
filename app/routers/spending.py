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
from app.services import commitments as commit_svc
from app.services import reports as report_svc
from app.templating import templates

router = APIRouter(prefix="/spending", tags=["spending"])


def _cashflow_ids(accounts):
    has_nc = any(not a.is_cashflow for a in accounts)
    return [a.id for a in accounts if a.is_cashflow] if has_nc else None


@router.get("", response_class=HTMLResponse)
async def spending_pulse(
    request: Request,
    period: str = Query("month"),
    ref: str = Query(""),
    rolling_start: str = Query(""),
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
    cashflow_ids = _cashflow_ids(accounts)

    pulse = await report_svc.weekly_spending_pulse(
        db, user.id, start, end, period=period, account_ids=cashflow_ids,
    )

    # Show commitments for the enclosing month (projection already done
    # inside weekly_spending_pulse, so rows exist by this point)
    m_start, m_end = report_svc.month_bounds(start.year, start.month)
    commitments = await commit_svc.get_commitments_for_period(
        db, user.id, m_start, m_end, include_cleared=True,
    )

    # Rolling over/under — default to start of current year
    rs_date = date.fromisoformat(rolling_start) if rolling_start else date(today.year, 1, 1)
    rolling = await report_svc.rolling_over_under(
        db, user.id, rs_date, end, account_ids=cashflow_ids,
    )

    prev_ref = report_svc.step_period(ref_date, -1, period)
    next_ref = report_svc.step_period(ref_date, 1, period)
    prev_url = f"/spending?period={period}&ref={prev_ref.isoformat()}"
    next_url = f"/spending?period={period}&ref={next_ref.isoformat()}"
    if rolling_start:
        prev_url += f"&rolling_start={rolling_start}"
        next_url += f"&rolling_start={rolling_start}"
    base_url = f"/spending?ref={ref_date.isoformat()}"
    if rolling_start:
        base_url += f"&rolling_start={rolling_start}"

    from app.services import categories as cat_svc
    all_cats = await cat_svc.get_category_tree(db, user.id)

    return templates.TemplateResponse(request, "spending/index.html", {
        "user": user,
        "period": period,
        "period_label": label,
        "start": start,
        "end": end,
        "pulse": pulse,
        "commitments": commitments,
        "all_categories": all_cats,
        "prev_url": prev_url,
        "next_url": next_url,
        "base_url": base_url,
        "rolling": rolling,
        "rolling_start": rs_date.isoformat(),
    })


# ── Commitment CRUD ────────────────────────────────────────────────

@router.post("/commitments/add")
async def add_commitment(
    request: Request,
    title: str = Form(...),
    amount: str = Form(...),
    due_date: str = Form(...),
    direction: str = Form("outflow"),
    category_id: str = Form(""),
    confidence: str = Form("confirmed"),
    is_recurring: str = Form(""),
    recurrence: str = Form(""),
    notes: str = Form(""),
    period: str = Form("week"),
    ref: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        amt = Decimal(amount)
    except (InvalidOperation, ValueError):
        amt = Decimal("0.00")

    cat_id = uuid.UUID(category_id) if category_id else None
    recurring = is_recurring == "on"

    await commit_svc.create_commitment(
        db, user.id,
        title=title.strip(),
        amount=amt,
        due_date=date.fromisoformat(due_date),
        direction=direction,
        category_id=cat_id,
        confidence=confidence,
        is_recurring=recurring,
        recurrence=recurrence if recurrence else None,
        notes=notes if notes else None,
    )
    await db.commit()

    return RedirectResponse(
        url=f"/spending?period={period}&ref={ref}",
        status_code=302,
    )


@router.post("/commitments/{commitment_id}/clear")
async def clear_commitment(
    commitment_id: uuid.UUID,
    period: str = Form("week"),
    ref: str = Form(""),
    amount: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    clear_amt = None
    if amount.strip():
        try:
            clear_amt = Decimal(amount)
        except (InvalidOperation, ValueError):
            pass
    await commit_svc.clear_commitment(db, commitment_id, user.id, amount=clear_amt)
    await db.commit()
    return RedirectResponse(
        url=f"/spending?period={period}&ref={ref}",
        status_code=302,
    )


@router.post("/commitments/{commitment_id}/delete")
async def delete_commitment(
    commitment_id: uuid.UUID,
    period: str = Form("week"),
    ref: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await commit_svc.delete_commitment(db, commitment_id, user.id)
    await db.commit()
    return RedirectResponse(
        url=f"/spending?period={period}&ref={ref}",
        status_code=302,
    )


# ── HTMX partials ──────────────────────────────────────────────────

@router.get("/category/__uncategorised__/transactions")
async def spending_uncategorised_txs(
    request: Request,
    period: str = Query("week"),
    ref: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period not in ("week", "month"):
        period = "week"

    today = date.today()
    ref_date = date.fromisoformat(ref) if ref else today
    start, end = report_svc.period_bounds(ref_date, period)

    accounts = await acct_svc.get_accounts(db, user.id)
    cf_ids = _cashflow_ids(accounts)

    txs = await report_svc.spending_uncategorised_transactions(
        db, user.id, start, end, account_ids=cf_ids,
    )

    if not txs:
        return HTMLResponse("<p class='text-sm text-gray-400 px-4 py-3'>No uncategorised transactions this period</p>")

    fmt = lambda v: f"${abs(v):,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
    rows = []
    for tx in txs:
        amt = tx["amount"]
        amt_class = "text-emerald-600" if amt >= 0 else "text-red-500"
        cleared_icon = (
            '<svg class="w-3 h-3 text-red-400" title="Reconciled" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6-6V7a6 6 0 1112 0v4M5 11h14a2 2 0 012 2v7a2 2 0 01-2 2H5a2 2 0 01-2-2v-7a2 2 0 012-2z"/></svg>'
        ) if tx["is_cleared"] else (
            '<svg class="w-3 h-3 text-emerald-400" title="Editable" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 11V7a4 4 0 118 0m-4 8v2m-6-6h14a2 2 0 012 2v7a2 2 0 01-2 2H5a2 2 0 01-2-2v-7a2 2 0 012-2z"/></svg>'
        )
        rows.append(
            f'<div class="flex items-center gap-2 px-4 py-2 border-t border-gray-100 dark:border-gray-700/50">'
            f'  <span class="flex-shrink-0">{cleared_icon}</span>'
            f'  <span class="text-xs text-gray-400 dark:text-gray-500 tabular-nums w-20">{tx["date"]}</span>'
            f'  <span class="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">{tx["description"]}</span>'
            f'  <span class="text-sm font-medium tabular-nums {amt_class} whitespace-nowrap">{fmt(amt)}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))


@router.get("/category/{category_id}/transactions")
async def spending_category_txs(
    category_id: uuid.UUID,
    request: Request,
    period: str = Query("week"),
    ref: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period not in ("week", "month"):
        period = "week"

    today = date.today()
    ref_date = date.fromisoformat(ref) if ref else today
    start, end = report_svc.period_bounds(ref_date, period)

    accounts = await acct_svc.get_accounts(db, user.id)
    cf_ids = _cashflow_ids(accounts)

    txs = await report_svc.spending_category_transactions(
        db, user.id, category_id, start, end, account_ids=cf_ids,
    )
    if txs is None:
        return HTMLResponse("<p class='text-sm text-gray-400 px-4 py-3'>Category not found</p>", status_code=404)

    # Also get commitments for this category
    from app.models.commitment import CommitmentDirection
    cat_commits = await commit_svc.get_commitments_for_period(
        db, user.id, start, end, direction=CommitmentDirection.OUTFLOW,
    )
    cat_commits = [c for c in cat_commits if c.category_id == category_id]

    fmt = lambda v: f"${abs(v):,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
    rows = []

    # Commitment rows (pending)
    for c in cat_commits:
        rows.append(
            f'<div class="flex items-center gap-2 px-4 py-2 border-t border-amber-100 dark:border-amber-900/30 bg-amber-50/50 dark:bg-amber-900/10">'
            f'  <span class="flex-shrink-0"><svg class="w-3 h-3 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg></span>'
            f'  <span class="text-xs text-amber-600 dark:text-amber-400 tabular-nums w-20">due {c.due_date.isoformat()}</span>'
            f'  <span class="text-sm text-amber-700 dark:text-amber-300 truncate flex-1">{c.title}</span>'
            f'  <span class="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase bg-amber-100 dark:bg-amber-900/40 text-amber-600 dark:text-amber-400">{c.confidence.value}</span>'
            f'  <span class="text-sm font-medium tabular-nums text-amber-600 dark:text-amber-400 whitespace-nowrap">-{fmt(float(c.amount))}</span>'
            f'</div>'
        )

    if not txs and not cat_commits:
        return HTMLResponse("<p class='text-sm text-gray-400 px-4 py-3'>No transactions or commitments this period</p>")

    # Actual transaction rows
    for tx in txs:
        amt = tx["amount"]
        amt_class = "text-emerald-600" if amt >= 0 else "text-red-500"
        cleared_icon = (
            '<svg class="w-3 h-3 text-red-400" title="Reconciled" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6-6V7a6 6 0 1112 0v4M5 11h14a2 2 0 012 2v7a2 2 0 01-2 2H5a2 2 0 01-2-2v-7a2 2 0 012-2z"/></svg>'
        ) if tx["is_cleared"] else (
            '<svg class="w-3 h-3 text-emerald-400" title="Editable" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 11V7a4 4 0 118 0m-4 8v2m-6-6h14a2 2 0 012 2v7a2 2 0 01-2 2H5a2 2 0 01-2-2v-7a2 2 0 012-2z"/></svg>'
        )
        rows.append(
            f'<div class="flex items-center gap-2 px-4 py-2 border-t border-gray-100 dark:border-gray-700/50">'
            f'  <span class="flex-shrink-0">{cleared_icon}</span>'
            f'  <span class="text-xs text-gray-400 dark:text-gray-500 tabular-nums w-20">{tx["date"]}</span>'
            f'  <span class="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">{tx["description"]}</span>'
            f'  <span class="text-sm font-medium tabular-nums {amt_class} whitespace-nowrap">{fmt(amt)}</span>'
            f'</div>'
        )
    return HTMLResponse("\n".join(rows))
