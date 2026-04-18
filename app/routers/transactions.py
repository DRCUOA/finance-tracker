import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import categories as cat_svc
from app.services import transactions as tx_svc
from app.services import categoriser
from app.services.transactions import DuplicateTransactionError
from app.services.reports import period_bounds
from app.templating import templates

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_class=HTMLResponse)
async def list_transactions(
    request: Request,
    account_id: str = Query(""),
    category_id: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    search: str = Query(""),
    sort_by: str = Query("date"),
    sort_dir: str = Query("desc"),
    page: int = Query(1),
    period: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period in ("week", "month") and not date_from:
        start, end = period_bounds(date.today(), period)
        date_from = start.isoformat()
        date_to = end.isoformat()

    aid = uuid.UUID(account_id) if account_id else None
    uncategorized = category_id == "__none__"
    cid = uuid.UUID(category_id) if category_id and not uncategorized else None
    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to) if date_to else None

    txs, total = await tx_svc.get_transactions(
        db, user.id, account_id=aid, category_id=cid,
        uncategorized=uncategorized,
        date_from=df, date_to=dt, search=search or None,
        sort_by=sort_by, sort_dir=sort_dir, page=page,
    )
    accounts = await acct_svc.get_accounts(db, user.id)
    cat_tree = await cat_svc.get_category_tree(db, user.id)
    total_pages = max(1, (total + 49) // 50)

    is_htmx = request.headers.get("HX-Request") == "true"
    template_name = "transactions/table.html" if is_htmx else "transactions/list.html"

    return templates.TemplateResponse(request, template_name, {
        "user": user,
        "transactions": txs, "total": total,
        "accounts": accounts, "category_tree": cat_tree,
        "page": page, "total_pages": total_pages,
        "account_id": account_id, "category_id": category_id,
        "date_from": date_from, "date_to": date_to,
        "search": search, "sort_by": sort_by, "sort_dir": sort_dir,
        "active_period": period,
    })


@router.get("/filtered-ids")
async def filtered_ids(
    account_id: str = Query(""),
    category_id: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    search: str = Query(""),
    period: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if period in ("week", "month") and not date_from:
        start, end = period_bounds(date.today(), period)
        date_from = start.isoformat()
        date_to = end.isoformat()

    aid = uuid.UUID(account_id) if account_id else None
    uncategorized = category_id == "__none__"
    cid = uuid.UUID(category_id) if category_id and not uncategorized else None
    df = date.fromisoformat(date_from) if date_from else None
    dt = date.fromisoformat(date_to) if date_to else None

    ids = await tx_svc.get_filtered_transaction_ids(
        db, user.id, account_id=aid, category_id=cid,
        uncategorized=uncategorized,
        date_from=df, date_to=dt, search=search or None,
    )
    return JSONResponse(content=ids)


@router.get("/create", response_class=HTMLResponse)
async def create_form(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    accounts = await acct_svc.get_accounts(db, user.id)
    cat_tree = await cat_svc.get_category_tree(db, user.id)
    return templates.TemplateResponse(request, "transactions/form.html", {
        "user": user,
        "tx": None, "accounts": accounts, "category_tree": cat_tree,
    })


@router.post("/create")
async def create_transaction(
    request: Request,
    account_id: str = Form(...),
    tx_date: str = Form(...),
    amount: str = Form(...),
    description: str = Form(...),
    category_id: str = Form(""),
    reference: str = Form(""),
    notes: str = Form(""),
    force: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        amt = Decimal(amount)
    except InvalidOperation:
        return RedirectResponse(url="/transactions/create", status_code=302)

    cid = uuid.UUID(category_id) if category_id else None
    if not cid:
        cid = await categoriser.suggest_category(db, user.id, description)

    try:
        tx = await tx_svc.create_transaction(
            db, user.id, uuid.UUID(account_id),
            date.fromisoformat(tx_date), amt, description,
            category_id=cid, reference=reference or None, notes=notes or None,
            force=bool(force),
        )
    except DuplicateTransactionError as exc:
        accounts = await acct_svc.get_accounts(db, user.id)
        cat_tree = await cat_svc.get_category_tree(db, user.id)
        return templates.TemplateResponse(request, "transactions/form.html", {
            "user": user,
            "tx": None, "accounts": accounts, "category_tree": cat_tree,
            "duplicate_warning": str(exc),
            "form_data": {
                "account_id": account_id, "tx_date": tx_date,
                "amount": amount, "description": description,
                "category_id": category_id, "reference": reference,
                "notes": notes,
            },
        })

    if cid:
        await categoriser.record_categorisation(db, user.id, cid, description)
    await acct_svc.recalculate_balance(db, uuid.UUID(account_id))
    return RedirectResponse(url="/transactions", status_code=302)


@router.post("/{tx_id}/update-category")
async def update_category_inline(
    tx_id: uuid.UUID,
    request: Request,
    category_id: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if await tx_svc.is_tx_locked(db, tx_id):
        return JSONResponse({"error": "Transaction is locked by reconciliation"}, status_code=403)
    cid = uuid.UUID(category_id) if category_id else None
    tx = await tx_svc.get_transaction(db, tx_id, user.id)
    if not tx:
        return JSONResponse({"error": "Not found"}, status_code=404)
    tx.category_id = cid
    await db.flush()
    if cid:
        await categoriser.record_categorisation(db, user.id, cid, tx.description)
    return JSONResponse({"ok": True})


@router.post("/batch/categorise")
async def batch_categorise(
    request: Request,
    category_id: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    tx_ids = [uuid.UUID(tid) for tid in form.getlist("tx_ids")]
    if tx_ids and category_id:
        await tx_svc.batch_categorise(db, tx_ids, user.id, uuid.UUID(category_id))
    resp = RedirectResponse(url="/transactions", status_code=302)
    if request.headers.get("HX-Request") == "true":
        resp.headers["HX-Trigger"] = "txchanged"
    return resp


@router.post("/batch/delete")
async def batch_delete(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    tx_ids = [uuid.UUID(tid) for tid in form.getlist("tx_ids")]
    if tx_ids:
        affected_accounts = set()
        for tid in tx_ids:
            tx = await tx_svc.get_transaction(db, tid, user.id)
            if tx:
                affected_accounts.add(tx.account_id)
        await tx_svc.batch_delete(db, tx_ids, user.id)
        for acct_id in affected_accounts:
            await acct_svc.recalculate_balance(db, acct_id)
    resp = RedirectResponse(url="/transactions", status_code=302)
    if request.headers.get("HX-Request") == "true":
        resp.headers["HX-Trigger"] = "txchanged"
    return resp


@router.get("/review-uncategorised", response_class=HTMLResponse)
async def review_uncategorised(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    matches, total_uncat = await categoriser.batch_suggest_categories(db, user.id)
    cat_tree = await cat_svc.get_category_tree(db, user.id)
    return templates.TemplateResponse(request, "transactions/review.html", {
        "user": user,
        "matches": matches,
        "total_uncategorised": total_uncat,
        "total_matched": len(matches),
        "category_tree": cat_tree,
    })


@router.post("/review-uncategorised/apply")
async def apply_review(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    applied = 0
    for key in form.keys():
        if key.startswith("cat_"):
            tx_id_str = key[4:]
            cat_id_str = form[key]
            if not cat_id_str:
                continue
            tx_id = uuid.UUID(tx_id_str)
            cat_id = uuid.UUID(cat_id_str)
            tx = await tx_svc.get_transaction(db, tx_id, user.id)
            if tx and tx.category_id is None:
                tx.category_id = cat_id
                await categoriser.record_categorisation(db, user.id, cat_id, tx.description)
                applied += 1
    await db.flush()
    return RedirectResponse(url="/transactions?review_applied=" + str(applied), status_code=302)


@router.get("/{tx_id}/detail")
async def transaction_detail(
    tx_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    data = await tx_svc.get_tx_detail(db, tx_id, user.id)
    if not data:
        return JSONResponse({"error": "Not found"}, status_code=404)
    cats = await cat_svc.get_category_tree(db, user.id)
    accts = await acct_svc.get_accounts(db, user.id)
    data["accounts"] = [{"id": str(a.id), "name": a.name} for a in accts]
    data["categories"] = []
    for parent in cats:
        data["categories"].append({"id": str(parent.id), "name": parent.name, "children": []})
        for child in (parent.children or []):
            data["categories"][-1]["children"].append({"id": str(child.id), "name": child.name})
    return data


@router.post("/{tx_id}/edit-modal")
async def edit_transaction_modal(
    tx_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if await tx_svc.is_tx_locked(db, tx_id):
        return JSONResponse({"error": "Transaction is locked by reconciliation"}, status_code=403)

    body = await request.json()
    kwargs = {}
    if "date" in body and body["date"]:
        kwargs["date"] = date.fromisoformat(body["date"])
    if "description" in body:
        kwargs["description"] = body["description"]
    if "amount" in body and body["amount"] is not None:
        kwargs["amount"] = Decimal(str(body["amount"]))
    if "account_id" in body and body["account_id"]:
        kwargs["account_id"] = uuid.UUID(body["account_id"])
    if "category_id" in body:
        kwargs["category_id"] = uuid.UUID(body["category_id"]) if body["category_id"] else None
    if "reference" in body:
        kwargs["reference"] = body["reference"] or None
    if "notes" in body:
        kwargs["notes"] = body["notes"] or None

    existing = await tx_svc.get_transaction(db, tx_id, user.id)
    if not existing:
        return JSONResponse({"error": "Not found"}, status_code=404)
    old_account_id = existing.account_id

    tx = await tx_svc.update_transaction(db, tx_id, user.id, **kwargs)
    if not tx:
        return JSONResponse({"error": "Not found"}, status_code=404)

    if kwargs.get("category_id"):
        await categoriser.record_categorisation(db, user.id, kwargs["category_id"], tx.description)

    accounts_to_recalc: set[uuid.UUID] = set()
    if old_account_id != tx.account_id:
        accounts_to_recalc.update({old_account_id, tx.account_id})
    elif "amount" in kwargs:
        accounts_to_recalc.add(tx.account_id)
    for acct_id in accounts_to_recalc:
        await acct_svc.recalculate_balance(db, acct_id)
    return JSONResponse({"ok": True})


@router.get("/{tx_id}/edit", response_class=HTMLResponse)
async def edit_form(
    tx_id: uuid.UUID, request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    tx = await tx_svc.get_transaction(db, tx_id, user.id)
    if not tx:
        return RedirectResponse(url="/transactions", status_code=302)
    accounts = await acct_svc.get_accounts(db, user.id)
    cat_tree = await cat_svc.get_category_tree(db, user.id)
    return templates.TemplateResponse(request, "transactions/form.html", {
        "user": user,
        "tx": tx, "accounts": accounts, "category_tree": cat_tree,
    })


@router.post("/{tx_id}/edit")
async def update_transaction(
    tx_id: uuid.UUID,
    request: Request,
    account_id: str = Form(...),
    tx_date: str = Form(...),
    amount: str = Form(...),
    description: str = Form(...),
    category_id: str = Form(""),
    reference: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if await tx_svc.is_tx_locked(db, tx_id):
        return RedirectResponse(url="/transactions", status_code=302)
    try:
        amt = Decimal(amount)
    except InvalidOperation:
        return RedirectResponse(url=f"/transactions/{tx_id}/edit", status_code=302)

    cid = uuid.UUID(category_id) if category_id else None
    new_account_id = uuid.UUID(account_id)
    existing = await tx_svc.get_transaction(db, tx_id, user.id)
    old_account_id = existing.account_id if existing else None
    await tx_svc.update_transaction(
        db, tx_id, user.id,
        account_id=new_account_id, date=date.fromisoformat(tx_date),
        amount=amt, description=description,
        category_id=cid, reference=reference or None, notes=notes or None,
    )
    if cid:
        await categoriser.record_categorisation(db, user.id, cid, description)
    await acct_svc.recalculate_balance(db, new_account_id)
    if old_account_id and old_account_id != new_account_id:
        await acct_svc.recalculate_balance(db, old_account_id)
    return RedirectResponse(url="/transactions", status_code=302)


@router.post("/{tx_id}/delete")
async def delete_transaction(
    tx_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if await tx_svc.is_tx_locked(db, tx_id):
        if request.headers.get("HX-Request") == "true":
            import json
            resp = HTMLResponse("", status_code=200)
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = json.dumps(
                {"showToast": {"message": "Cannot delete — locked by reconciliation", "type": "error"}}
            )
            return resp
        return RedirectResponse(url="/transactions", status_code=302)
    tx = await tx_svc.get_transaction(db, tx_id, user.id)
    if tx:
        acct_id = tx.account_id
        await tx_svc.delete_transaction(db, tx_id, user.id)
        await acct_svc.recalculate_balance(db, acct_id)
    if request.headers.get("HX-Request") == "true":
        resp = HTMLResponse("")
        resp.headers["HX-Trigger"] = "txchanged"
        return resp
    return RedirectResponse(url="/transactions", status_code=302)
