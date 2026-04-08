import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.schemas.matching_rules import KeywordUpdateBody
from app.services import categories as cat_svc
from app.services import matching_rules as mr_svc
from app.services import transactions as tx_svc
from app.templating import templates

router = APIRouter(prefix="/matching-rules", tags=["matching-rules"])


def _rules_for_json(rules: list[dict]) -> list[dict]:
    return [
        {
            **r,
            "keyword_id": str(r["keyword_id"]),
            "category_id": str(r["category_id"]),
        }
        for r in rules
    ]


@router.get("", response_class=HTMLResponse)
async def matching_rules_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    rules = await mr_svc.list_rules(db, user.id)
    unmatched, unmatched_total = await tx_svc.get_transactions(
        db, user.id, uncategorized=True, page=1, per_page=80,
    )
    tree = await cat_svc.get_category_tree(db, user.id)
    return templates.TemplateResponse(request, "matching_rules/index.html", {
        "user": user,
        "rules": rules,
        "rules_json": _rules_for_json(rules),
        "unmatched": unmatched,
        "unmatched_total": unmatched_total,
        "category_tree": tree,
        "search": "",
    })


@router.get("/partials/unmatched", response_class=HTMLResponse)
async def partial_unmatched(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    search: str = Query(""),
):
    unmatched, unmatched_total = await tx_svc.get_transactions(
        db, user.id, uncategorized=True, page=1, per_page=80,
        search=search or None,
    )
    return templates.TemplateResponse(request, "matching_rules/_unmatched.html", {
        "unmatched": unmatched,
        "unmatched_total": unmatched_total,
        "search": search,
    })


@router.get("/api/preview-count")
async def preview_count(
    phrase: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    n = await mr_svc.count_uncategorized_matching(db, user.id, phrase)
    return JSONResponse({"count": n})


@router.post("/rules")
async def add_rule(
    category_id: uuid.UUID = Form(...),
    keyword: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    keyword = keyword.strip()
    if keyword:
        await cat_svc.add_keyword(db, category_id, user.id, keyword)
    return RedirectResponse(url="/matching-rules", status_code=302)


@router.post("/rules/{keyword_id}/delete")
async def delete_rule(
    keyword_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await cat_svc.delete_keyword(db, keyword_id, user.id)
    return RedirectResponse(url="/matching-rules", status_code=302)


@router.get("/health", response_class=HTMLResponse)
async def keyword_health(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    report = await mr_svc.keyword_health_report(db, user.id)
    return templates.TemplateResponse(request, "matching_rules/_health.html", {
        "report": report,
    })


@router.patch("/rules/{keyword_id}")
async def update_rule(
    keyword_id: uuid.UUID,
    body: KeywordUpdateBody,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    updated = await cat_svc.update_keyword(db, keyword_id, user.id, body.keyword)
    if updated is None:
        return JSONResponse({"ok": False, "error": "Not found or duplicate keyword"}, status_code=400)
    return JSONResponse({"ok": True, "keyword": updated.keyword})
