import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.commitment import CommitmentConfidence, CommitmentDirection, CommitmentRecurrence
from app.models.user import User
from app.routers.auth import require_user
from app.services import categories as cat_svc
from app.services import commitments as commit_svc
from app.templating import templates

router = APIRouter(prefix="/commitments", tags=["commitments"])


@router.get("", response_class=HTMLResponse)
async def commitments_hub(
    request: Request,
    tab: str = Query("pending"),
    direction: str = Query(""),
    confidence: str = Query(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if tab not in ("all", "pending", "upcoming", "overdue", "recurring", "cleared"):
        tab = "pending"

    commitments = await commit_svc.get_all_commitments(
        db, user.id,
        status=tab,
        direction=direction or None,
        confidence=confidence or None,
    )
    summary = await commit_svc.get_commitment_summary(db, user.id)
    all_cats = await cat_svc.get_category_tree(db, user.id)

    return templates.TemplateResponse(request, "commitments/index.html", {
        "user": user,
        "commitments": commitments,
        "summary": summary,
        "all_categories": all_cats,
        "tab": tab,
        "filter_direction": direction,
        "filter_confidence": confidence,
        "today": date.today(),
    })


@router.post("/add")
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
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        amt = Decimal(amount)
    except (InvalidOperation, ValueError):
        amt = Decimal("0.00")

    await commit_svc.create_commitment(
        db, user.id,
        title=title.strip(),
        amount=amt,
        due_date=date.fromisoformat(due_date),
        direction=direction,
        category_id=uuid.UUID(category_id) if category_id else None,
        confidence=confidence,
        is_recurring=is_recurring == "on",
        recurrence=recurrence or None,
        notes=notes or None,
    )
    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/{commitment_id}/edit")
async def edit_commitment(
    commitment_id: uuid.UUID,
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
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        amt = Decimal(amount)
    except (InvalidOperation, ValueError):
        amt = Decimal("0.00")

    await commit_svc.update_commitment(
        db, commitment_id, user.id,
        title=title.strip(),
        amount=amt,
        due_date=date.fromisoformat(due_date),
        direction=CommitmentDirection(direction),
        category_id=uuid.UUID(category_id) if category_id else None,
        confidence=CommitmentConfidence(confidence),
        is_recurring=is_recurring == "on",
        recurrence=CommitmentRecurrence(recurrence) if recurrence else None,
        notes=notes or None,
    )
    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/{commitment_id}/clear")
async def clear_commitment(
    commitment_id: uuid.UUID,
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
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/{commitment_id}/unclear")
async def unclear_commitment(
    commitment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await commit_svc.unclear_commitment(db, commitment_id, user.id)
    await db.commit()
    return RedirectResponse(url="/commitments?tab=pending", status_code=302)


@router.post("/{commitment_id}/delete")
async def delete_commitment(
    commitment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    await commit_svc.delete_commitment(db, commitment_id, user.id)
    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/project-recurring")
async def project_recurring(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    through = date.today() + timedelta(days=90)
    count = await commit_svc.project_recurring_commitments(db, user.id, through)
    await db.commit()
    return RedirectResponse(url="/commitments?tab=recurring", status_code=302)


@router.post("/wizard/recurring-bills")
async def wizard_recurring_bills(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    count = int(form.get("bill_count", "0"))

    for i in range(count):
        title = form.get(f"bill_{i}_title", "").strip()
        amount_str = form.get(f"bill_{i}_amount", "0")
        day = int(form.get(f"bill_{i}_day", "1"))
        cat_id_str = form.get(f"bill_{i}_category", "")
        rec = form.get(f"bill_{i}_recurrence", "monthly")

        if not title:
            continue
        try:
            amt = Decimal(amount_str)
        except (InvalidOperation, ValueError):
            continue

        today = date.today()
        due = date(today.year, today.month, min(day, 28))
        if due < today:
            due = commit_svc._next_due_date(due, CommitmentRecurrence(rec))

        await commit_svc.create_commitment(
            db, user.id,
            title=title, amount=amt, due_date=due,
            direction="outflow",
            category_id=uuid.UUID(cat_id_str) if cat_id_str else None,
            confidence="confirmed",
            is_recurring=True,
            recurrence=rec,
        )

    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/wizard/annual-expense")
async def wizard_annual_expense(
    request: Request,
    title: str = Form(...),
    amount: str = Form(...),
    due_date: str = Form(...),
    category_id: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        amt = Decimal(amount)
    except (InvalidOperation, ValueError):
        amt = Decimal("0.00")

    await commit_svc.create_commitment(
        db, user.id,
        title=title.strip(),
        amount=amt,
        due_date=date.fromisoformat(due_date),
        direction="outflow",
        category_id=uuid.UUID(category_id) if category_id else None,
        confidence="confirmed",
        is_recurring=True,
        recurrence="annually",
        notes=notes or None,
    )
    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/wizard/event-budget")
async def wizard_event_budget(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    event_name = form.get("event_name", "").strip()
    event_date_str = form.get("event_date", "")
    count = int(form.get("item_count", "0"))

    event_date = date.fromisoformat(event_date_str) if event_date_str else date.today()

    for i in range(count):
        item = form.get(f"item_{i}_title", "").strip()
        amount_str = form.get(f"item_{i}_amount", "0")
        conf = form.get(f"item_{i}_confidence", "expected")
        cat_id_str = form.get(f"item_{i}_category", "")
        item_date_str = form.get(f"item_{i}_due_date", "").strip()

        if not item:
            continue
        try:
            amt = Decimal(amount_str)
        except (InvalidOperation, ValueError):
            continue

        item_due = date.fromisoformat(item_date_str) if item_date_str else event_date
        full_title = f"{event_name}: {item}" if event_name else item

        await commit_svc.create_commitment(
            db, user.id,
            title=full_title, amount=amt, due_date=item_due,
            direction="outflow",
            category_id=uuid.UUID(cat_id_str) if cat_id_str else None,
            confidence=conf,
        )

    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)


@router.post("/wizard/review-history/analyze")
async def wizard_review_history_analyze(
    request: Request,
    date_from: str = Form(...),
    date_to: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    suggestions = await commit_svc.analyze_history(db, user.id, start, end)
    return JSONResponse({"suggestions": suggestions})


@router.post("/wizard/review-history/create")
async def wizard_review_history_create(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    count = int(form.get("suggestion_count", "0"))

    for i in range(count):
        selected = form.get(f"sug_{i}_selected", "")
        if selected != "on":
            continue

        title = form.get(f"sug_{i}_title", "").strip()
        amount_str = form.get(f"sug_{i}_amount", "0")
        direction = form.get(f"sug_{i}_direction", "outflow")
        cat_id_str = form.get(f"sug_{i}_category_id", "")
        confidence = form.get(f"sug_{i}_confidence", "expected")
        is_recurring = form.get(f"sug_{i}_is_recurring", "") == "on"
        recurrence = form.get(f"sug_{i}_recurrence", "")

        if not title:
            continue
        try:
            amt = Decimal(amount_str)
        except (InvalidOperation, ValueError):
            continue

        today = date.today()
        if is_recurring and recurrence:
            due = today + timedelta(days=1)
        else:
            due = today

        await commit_svc.create_commitment(
            db, user.id,
            title=title, amount=amt, due_date=due,
            direction=direction,
            category_id=uuid.UUID(cat_id_str) if cat_id_str else None,
            confidence=confidence,
            is_recurring=is_recurring,
            recurrence=recurrence or None,
        )

    await db.commit()
    return RedirectResponse(url="/commitments", status_code=302)
