import json
import uuid
from datetime import date

from app.dates import fmt_date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.services import budgets as budget_svc
from app.templating import templates

router = APIRouter(prefix="/budgets", tags=["budgets"])


@router.get("", include_in_schema=False)
async def budgets_index():
    return RedirectResponse(url="/budgets/setup", status_code=302)


@router.get("/setup", response_class=HTMLResponse)
async def budget_setup(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    suggestions = await budget_svc.get_budget_suggestions(db, user.id)
    avg_income = await budget_svc.get_income_average(db, user.id)
    income_cats = await budget_svc.get_income_categories(db, user.id)

    today = date.today()
    overrides = await budget_svc.get_month_overrides(db, user.id, today.year, today.month)

    return templates.TemplateResponse(request, "budgets/setup.html", {
        "user": user,
        "suggestions": suggestions,
        "avg_income": avg_income,
        "income_cats": income_cats,
        "overrides": overrides,
        "current_year": today.year,
        "current_month": today.month,
        "month_label": fmt_date(today, "month"),
    })


@router.post("/apply-defaults")
async def apply_default_budgets(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-update default budgets and income categories."""
    body = await request.json()
    budgets = body.get("budgets", {})
    income = body.get("income", [])

    count = 0
    if budgets:
        count = await budget_svc.bulk_set_default_budgets(db, user.id, budgets)

    income_ids = []
    if income:
        income_ids = await budget_svc.save_income_lines(db, user.id, income)

    await db.commit()

    parts = []
    if count:
        parts.append(f"{count} budgets")
    if income_ids:
        parts.append(f"{len(income_ids)} income sources")
    msg = "Updated " + " and ".join(parts) if parts else "No changes"

    trigger = json.dumps({
        "notify": {"message": msg, "type": "success"}
    })
    return Response(
        status_code=200,
        content=json.dumps({"ok": True, "count": count, "income_ids": income_ids}),
        media_type="application/json",
        headers={"HX-Trigger": trigger},
    )


@router.post("/month-override")
async def set_month_override(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Set or update a monthly budget override for a category."""
    body = await request.json()
    try:
        cat_id = uuid.UUID(body["category_id"])
        year = int(body["year"])
        month = int(body["month"])
        amount = Decimal(str(body["amount"]))
    except (KeyError, ValueError, InvalidOperation):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    await budget_svc.set_month_override(db, user.id, cat_id, year, month, amount)
    await db.commit()

    trigger = json.dumps({
        "notify": {"message": "Monthly budget updated", "type": "success"}
    })
    return Response(
        status_code=200,
        content=json.dumps({"ok": True}),
        media_type="application/json",
        headers={"HX-Trigger": trigger},
    )


@router.delete("/month-override")
async def delete_month_override(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a monthly budget override, reverting to the category default."""
    body = await request.json()
    try:
        cat_id = uuid.UUID(body["category_id"])
        year = int(body["year"])
        month = int(body["month"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    await budget_svc.clear_month_override(db, user.id, cat_id, year, month)
    await db.commit()

    trigger = json.dumps({
        "notify": {"message": "Override removed — using default", "type": "success"}
    })
    return Response(
        status_code=200,
        content=json.dumps({"ok": True}),
        media_type="application/json",
        headers={"HX-Trigger": trigger},
    )


@router.post("/copy-month")
async def copy_month(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Copy all budget overrides from one month to another."""
    body = await request.json()
    try:
        src_year = int(body["source_year"])
        src_month = int(body["source_month"])
        tgt_year = int(body["target_year"])
        tgt_month = int(body["target_month"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    count = await budget_svc.copy_budgets_from_month(
        db, user.id, src_year, src_month, tgt_year, tgt_month,
    )
    await db.commit()

    trigger = json.dumps({
        "notify": {
            "message": f"Copied {count} budget overrides" if count else "No overrides to copy from that month",
            "type": "success" if count else "warning",
        }
    })
    return Response(
        status_code=200,
        content=json.dumps({"ok": True, "count": count}),
        media_type="application/json",
        headers={"HX-Trigger": trigger},
    )


@router.get("/suggestions")
async def get_suggestions(
    request: Request,
    periods: int = Query(3, ge=1, le=12),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """JSON endpoint returning category spending averages for budget suggestions."""
    suggestions = await budget_svc.get_budget_suggestions(db, user.id, periods=periods)
    avg_income = await budget_svc.get_income_average(db, user.id, periods=periods)
    return {"suggestions": suggestions, "avg_income": avg_income}
