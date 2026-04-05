import json
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.statement import FileType
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import import_service as import_svc
from app.services import migration as migration_svc
from app.templating import templates

_MIGRATE_DIR = Path(tempfile.gettempdir()) / "finance_tracker_migrate"

router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("", response_class=HTMLResponse)
async def import_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id)
    return templates.TemplateResponse(request, "imports/upload.html", {
        "user": user, "accounts": accounts,
    })


@router.post("/upload")
async def upload_file(
    request: Request,
    account_id: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    content_bytes = await file.read()
    filename = file.filename or "upload"
    is_ofx = filename.lower().endswith((".ofx", ".qfx"))

    if is_ofx:
        parsed = import_svc.parse_ofx(content_bytes)
        parsed = await import_svc.find_duplicates(db, user.id, uuid.UUID(account_id), parsed)
        statement = await import_svc.create_statement(
            db, user.id, uuid.UUID(account_id), filename, FileType.OFX, parsed,
        )
        return templates.TemplateResponse(request, "imports/review.html", {
            "user": user,
            "statement": statement, "transactions": parsed,
            "account_id": account_id,
        })
    else:
        content = content_bytes.decode("utf-8", errors="replace")
        preview = import_svc.parse_csv_preview(content)
        return templates.TemplateResponse(request, "imports/map_fields.html", {
            "user": user,
            "preview": preview, "account_id": account_id,
            "filename": filename, "csv_content": content,
        })


@router.post("/map")
async def map_csv_fields(
    request: Request,
    account_id: str = Form(...),
    filename: str = Form(...),
    csv_content: str = Form(...),
    date_col: int = Form(...),
    amount_col: int = Form(...),
    desc_col: int = Form(...),
    ref_col: str = Form(""),
    date_format: str = Form("%Y-%m-%d"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    ref = int(ref_col) if ref_col else None
    parsed = import_svc.parse_csv_transactions(csv_content, date_col, amount_col, desc_col, ref, date_format)
    parsed = await import_svc.find_duplicates(db, user.id, uuid.UUID(account_id), parsed)
    statement = await import_svc.create_statement(
        db, user.id, uuid.UUID(account_id), filename, FileType.CSV, parsed,
    )
    return templates.TemplateResponse(request, "imports/review.html", {
        "user": user,
        "statement": statement, "transactions": parsed,
        "account_id": account_id,
    })


@router.post("/confirm")
async def confirm_import(
    request: Request,
    statement_id: str = Form(...),
    account_id: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    line_ids = [uuid.UUID(lid) for lid in form.getlist("line_ids")]
    result = await import_svc.import_statement_lines(
        db, user.id, uuid.UUID(statement_id), line_ids, uuid.UUID(account_id),
    )
    await acct_svc.recalculate_balance(db, uuid.UUID(account_id))
    return templates.TemplateResponse(request, "imports/done.html", {
        "user": user,
        "count": result.imported,
        "skipped": result.skipped,
        "skipped_descriptions": result.skipped_descriptions,
    })


# ---------------------------------------------------------------------------
# Migration from external JSON export
# ---------------------------------------------------------------------------

SKIP_ROOTS_DEFAULT = {"Home Loan", "House Value Adjustments"}


@router.get("/migrate", response_class=HTMLResponse)
async def migrate_page(
    request: Request,
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(request, "imports/migrate_upload.html", {
        "user": user,
    })


@router.post("/migrate/preview")
async def migrate_preview(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
):
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return templates.TemplateResponse(request, "imports/migrate_upload.html", {
            "user": user, "error": "Invalid JSON file.",
        })

    preview = migration_svc.preview_external_data(data, skip_roots=SKIP_ROOTS_DEFAULT)

    _MIGRATE_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    (_MIGRATE_DIR / f"{token}.json").write_bytes(content)

    return templates.TemplateResponse(request, "imports/migrate_preview.html", {
        "user": user,
        "preview": preview,
        "skip_roots": sorted(SKIP_ROOTS_DEFAULT),
        "token": token,
    })


@router.post("/migrate/confirm")
async def migrate_confirm(
    request: Request,
    token: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    selected = set(form.getlist("account_ids"))
    include_uncat = set(form.getlist("include_uncategorized"))

    staged = _MIGRATE_DIR / f"{token}.json"
    if not staged.exists():
        return RedirectResponse(url="/imports/migrate", status_code=302)

    data = json.loads(staged.read_text())
    staged.unlink(missing_ok=True)

    result = await migration_svc.import_external_data(
        db, user.id, data,
        account_ids=selected,
        skip_category_roots=SKIP_ROOTS_DEFAULT,
        include_uncategorized_for=include_uncat & selected,
    )

    return templates.TemplateResponse(request, "imports/migrate_done.html", {
        "user": user, "result": result,
    })
