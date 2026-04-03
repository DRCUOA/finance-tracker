import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.category import CategoryType
from app.models.user import User
from app.routers.auth import require_user
from app.services import categories as cat_svc
from app.templating import templates

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_class=HTMLResponse)
async def list_categories(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    tree = await cat_svc.get_category_tree(db, user.id)
    return templates.TemplateResponse(request, "categories/list.html", {
        "user": user, "tree": tree,
        "category_types": CategoryType,
    })


@router.post("/create")
async def create_category(
    request: Request,
    name: str = Form(...),
    category_type: str = Form(...),
    parent_id: str = Form(""),
    budgeted_amount: str = Form("0.00"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    pid = uuid.UUID(parent_id) if parent_id else None
    try:
        ba = Decimal(budgeted_amount)
    except InvalidOperation:
        ba = Decimal("0.00")
    await cat_svc.create_category(db, user.id, name, CategoryType(category_type), pid, ba)
    return RedirectResponse(url="/categories", status_code=302)


@router.post("/{category_id}/edit")
async def update_category(
    category_id: uuid.UUID,
    name: str = Form(...),
    budgeted_amount: str = Form("0.00"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        ba = Decimal(budgeted_amount)
    except InvalidOperation:
        ba = Decimal("0.00")
    await cat_svc.update_category(db, category_id, user.id, name=name, budgeted_amount=ba)
    return RedirectResponse(url="/categories", status_code=302)


@router.post("/{category_id}/delete")
async def delete_category(
    category_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await cat_svc.delete_category(db, category_id, user.id)
    return RedirectResponse(url="/categories", status_code=302)


@router.post("/{category_id}/keywords/add")
async def add_keyword(
    category_id: uuid.UUID,
    keyword: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await cat_svc.add_keyword(db, category_id, user.id, keyword)
    return RedirectResponse(url="/categories", status_code=302)


@router.post("/keywords/{keyword_id}/delete")
async def delete_keyword(
    keyword_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await cat_svc.delete_keyword(db, keyword_id, user.id)
    return RedirectResponse(url="/categories", status_code=302)
