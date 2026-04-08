import json
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.database import get_db
from app.models.category import CategoryType
from app.models.user import User
from app.routers.auth import require_user
from app.schemas.category import InlineCategoryCreate, InlineCategoryUpdate, KeywordSync
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


@router.get("/{category_id}/detail")
async def category_detail(
    category_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    cat = await cat_svc.get_category(db, category_id, user.id)
    if not cat:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {
        "id": str(cat.id),
        "name": cat.name,
        "category_type": cat.category_type.value,
        "budgeted_amount": float(cat.budgeted_amount),
        "parent_id": str(cat.parent_id) if cat.parent_id else "",
        "keywords": [{"id": str(kw.id), "keyword": kw.keyword} for kw in (cat.keywords or [])],
    }


@router.post("/{category_id}/edit-modal")
async def edit_category_modal(
    category_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    kwargs = {}
    if "name" in body and body["name"]:
        kwargs["name"] = body["name"]
    if "budgeted_amount" in body and body["budgeted_amount"] is not None:
        kwargs["budgeted_amount"] = Decimal(str(body["budgeted_amount"]))
    if not kwargs:
        return JSONResponse({"error": "Nothing to update"}, status_code=400)
    result = await cat_svc.update_category(db, category_id, user.id, **kwargs)
    if result is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"ok": True})


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
    keyword: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    keyword = keyword.strip()
    if not keyword:
        return RedirectResponse(url="/categories", status_code=302)
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


@router.post("/inline-create")
async def inline_create_category(
    body: InlineCategoryCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    pid = uuid.UUID(body.parent_id) if body.parent_id else None
    cat = await cat_svc.create_category(
        db, user.id, body.name, CategoryType(body.category_type),
        pid, body.budgeted_amount,
    )
    return Response(
        status_code=201,
        content=json.dumps({"id": str(cat.id)}),
        media_type="application/json",
        headers={"HX-Trigger": json.dumps({"notify": {"message": "Category created", "type": "success"}})},
    )


@router.delete("/{category_id}/inline")
async def inline_delete_category(
    category_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await cat_svc.delete_category(db, category_id, user.id)
    if not ok:
        return Response(status_code=404)
    trigger = json.dumps({"notify": {"message": "Deleted", "type": "success"}})
    return Response(status_code=204, headers={"HX-Trigger": trigger})


@router.patch("/{category_id}/inline")
async def inline_update_category(
    category_id: uuid.UUID,
    body: InlineCategoryUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return Response(status_code=204)
    result = await cat_svc.update_category(db, category_id, user.id, **updates)
    if result is None:
        return Response(status_code=404)
    trigger = json.dumps({"notify": {"message": "Saved", "type": "success"}})
    return Response(status_code=204, headers={"HX-Trigger": trigger})


@router.post("/{source_id}/merge-into/{target_id}")
async def merge_category(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await cat_svc.merge_categories(db, source_id, target_id, user.id)
    if result is None:
        return JSONResponse({"error": "Invalid merge"}, status_code=400)
    trigger = json.dumps({
        "notify": {
            "message": f"Merged {result['source_name']} into {result['target_name']} ({result['transactions_moved']} transactions moved)",
            "type": "success",
        }
    })
    return Response(
        status_code=200,
        content=json.dumps({"ok": True, **result}),
        media_type="application/json",
        headers={"HX-Trigger": trigger, "HX-Redirect": "/categories"},
    )


@router.put("/{category_id}/keywords")
async def sync_keywords(
    category_id: uuid.UUID,
    body: KeywordSync,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await cat_svc.sync_keywords(db, category_id, user.id, body.keywords)
    if not ok:
        return Response(status_code=404)
    trigger = json.dumps({"notify": {"message": "Keywords saved", "type": "success"}})
    return Response(status_code=204, headers={"HX-Trigger": trigger})
