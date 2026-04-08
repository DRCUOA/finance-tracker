from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import categories as cat_svc
from app.services.sql_tool import execute_query
from app.templating import templates

router = APIRouter(prefix="/sql", tags=["sql-tool"])


def _build_lookups(accounts, category_tree):
    acct_list = [{"id": str(a.id), "name": a.name} for a in accounts]
    cat_list = []
    for parent in category_tree:
        cat_list.append({"id": str(parent.id), "name": parent.name})
        for child in sorted(parent.children, key=lambda c: c.sort_order):
            cat_list.append({"id": str(child.id), "name": f"{parent.name} › {child.name}"})
    return acct_list, cat_list


@router.get("", response_class=HTMLResponse)
async def sql_tool_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id, active_only=False)
    category_tree = await cat_svc.get_category_tree(db, user.id)
    acct_lookup, cat_lookup = _build_lookups(accounts, category_tree)

    return templates.TemplateResponse(request, "sql_tool/index.html", {
        "user": user,
        "result": None,
        "error": None,
        "query": "",
        "acct_lookup": acct_lookup,
        "cat_lookup": cat_lookup,
    })


@router.post("/execute", response_class=HTMLResponse)
async def sql_execute(
    request: Request,
    query: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    error = None
    result = None
    try:
        result = await execute_query(db, user.id, query)
    except (ValueError, Exception) as exc:
        error = str(exc)

    is_htmx = request.headers.get("HX-Request") == "true"
    template = "sql_tool/_results.html" if is_htmx else "sql_tool/index.html"

    ctx = {
        "user": user,
        "result": result,
        "error": error,
        "query": query,
    }

    if not is_htmx:
        accounts = await acct_svc.get_accounts(db, user.id, active_only=False)
        category_tree = await cat_svc.get_category_tree(db, user.id)
        ctx["acct_lookup"], ctx["cat_lookup"] = _build_lookups(accounts, category_tree)

    return templates.TemplateResponse(request, template, ctx)
