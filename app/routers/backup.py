import json
import uuid
from typing import List

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.services import backup as backup_svc
from app.services import accounts as acct_svc
from app.services import categories as cat_svc
from app.services import transactions as tx_svc
from app.templating import templates

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("", response_class=HTMLResponse)
async def backup_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id, active_only=False)
    return templates.TemplateResponse(request, "backup/index.html", {
        "user": user, "accounts": accounts,
    })


@router.get("/export/full")
async def export_full_json(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    data = await backup_svc.full_backup(db, user.id)
    content = json.dumps(data, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=finla-backup.json"},
    )


@router.post("/restore")
async def restore_backup(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    data = json.loads(content)
    stats = await backup_svc.restore_backup(db, user.id, data)
    return templates.TemplateResponse(request, "backup/restore_done.html", {
        "user": user, "stats": stats,
    })


@router.get("/export/transactions")
async def export_transactions(
    fmt: str = "csv",
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    txs, _ = await tx_svc.get_transactions(db, user.id, per_page=100000)
    rows = [
        {
            "date": tx.date.isoformat(), "amount": float(tx.amount),
            "description": tx.description, "account": tx.account.name if tx.account else "",
            "category": tx.category.name if tx.category else "",
            "reference": tx.reference or "", "is_cleared": tx.is_cleared,
        }
        for tx in txs
    ]
    if fmt == "json":
        content = backup_svc.export_table_json(rows)
        return Response(content=content, media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=transactions.json"})
    content = backup_svc.export_table_csv(rows)
    return Response(content=content, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions.csv"})


@router.get("/export/accounts")
async def export_accounts(
    fmt: str = "csv",
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id, active_only=False)
    rows = [
        {
            "name": a.name, "type": a.account_type.value, "currency": a.currency,
            "initial_balance": float(a.initial_balance), "current_balance": float(a.current_balance),
            "institution": a.institution or "", "is_active": a.is_active,
        }
        for a in accounts
    ]
    if fmt == "json":
        content = backup_svc.export_table_json(rows)
        return Response(content=content, media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=accounts.json"})
    content = backup_svc.export_table_csv(rows)
    return Response(content=content, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=accounts.csv"})


@router.post("/export/account-bundle")
async def export_account_bundle(
    request: Request,
    account_ids: List[str] = Form(...),
    mode: str = Form("schema"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    ids = [uuid.UUID(aid) for aid in account_ids]
    include_data = mode == "full"
    data = await backup_svc.export_account_bundle(db, user.id, ids, include_data=include_data)
    content = json.dumps(data, indent=2, default=str)
    suffix = "full" if include_data else "schema"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=accounts-{suffix}.json"},
    )


@router.post("/import/account-bundle")
async def import_account_bundle(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    data = json.loads(content)
    stats = await backup_svc.import_account_bundle(db, user.id, data)
    return templates.TemplateResponse(request, "backup/import_done.html", {
        "user": user, "stats": stats,
    })


@router.get("/export/matching-rules")
async def export_matching_rules(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    data = await backup_svc.export_matching_rules(db, user.id)
    content = json.dumps(data, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=matching-rules.json"},
    )


@router.post("/import/matching-rules")
async def import_matching_rules(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    data = json.loads(content)
    stats = await backup_svc.import_matching_rules(db, user.id, data)
    return templates.TemplateResponse(request, "backup/rules_import_done.html", {
        "user": user, "stats": stats,
    })


@router.get("/export/categories")
async def export_category_bundle(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    data = await backup_svc.export_category_bundle(db, user.id)
    content = json.dumps(data, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=categories.json"},
    )


@router.post("/import/categories")
async def import_category_bundle(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    data = json.loads(content)
    stats = await backup_svc.import_category_bundle(db, user.id, data)
    return templates.TemplateResponse(request, "backup/categories_import_done.html", {
        "user": user, "stats": stats,
    })
