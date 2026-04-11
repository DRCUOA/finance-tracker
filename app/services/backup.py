import csv
import io
import json
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.account import Account
from app.models.budget import Budget
from app.models.category import Category, CategoryKeyword
from app.models.commitment import Commitment
from app.models.transaction import Transaction


def _serialize(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Not serializable: {type(obj)}")


async def full_backup(db: AsyncSession, user_id: uuid.UUID) -> dict:
    accounts = (await db.execute(
        select(Account).where(Account.user_id == user_id).order_by(Account.sort_order)
    )).scalars().all()

    categories = (await db.execute(
        select(Category).where(Category.user_id == user_id)
        .options(selectinload(Category.keywords))
        .order_by(Category.sort_order)
    )).scalars().all()

    transactions = (await db.execute(
        select(Transaction).where(Transaction.user_id == user_id)
        .order_by(Transaction.date)
    )).scalars().all()

    budgets = (await db.execute(
        select(Budget).where(Budget.user_id == user_id)
    )).scalars().all()

    commitments = (await db.execute(
        select(Commitment).where(Commitment.user_id == user_id)
        .order_by(Commitment.due_date)
    )).scalars().all()

    return {
        "version": "1.1",
        "exported_at": datetime.utcnow().isoformat(),
        "accounts": [
            {
                "id": str(a.id), "name": a.name, "account_type": a.account_type.value,
                "currency": a.currency, "initial_balance": float(a.initial_balance),
                "institution": a.institution, "is_cashflow": a.is_cashflow,
                "is_active": a.is_active, "sort_order": a.sort_order,
            }
            for a in accounts
        ],
        "categories": [
            {
                "id": str(c.id), "name": c.name, "category_type": c.category_type.value,
                "parent_id": str(c.parent_id) if c.parent_id else None,
                "sort_order": c.sort_order, "budgeted_amount": float(c.budgeted_amount),
                "reserve_amount": float(c.reserve_amount), "is_fixed": c.is_fixed,
                "keywords": [{"keyword": kw.keyword, "hit_count": kw.hit_count} for kw in c.keywords],
            }
            for c in categories
        ],
        "transactions": [
            {
                "id": str(t.id), "account_id": str(t.account_id),
                "category_id": str(t.category_id) if t.category_id else None,
                "date": t.date.isoformat(), "amount": float(t.amount),
                "description": t.description,
                "original_description": t.original_description,
                "reference": t.reference, "notes": t.notes,
                "is_cleared": t.is_cleared,
            }
            for t in transactions
        ],
        "budgets": [
            {
                "id": str(b.id), "category_id": str(b.category_id),
                "year": b.year, "month": b.month, "amount": float(b.amount),
            }
            for b in budgets
        ],
        "commitments": [
            {
                "id": str(cm.id), "category_id": str(cm.category_id) if cm.category_id else None,
                "title": cm.title, "amount": float(cm.amount),
                "direction": cm.direction.value, "due_date": cm.due_date.isoformat(),
                "is_recurring": cm.is_recurring,
                "recurrence": cm.recurrence.value if cm.recurrence else None,
                "confidence": cm.confidence.value,
                "is_active": cm.is_active,
                "cleared_at": cm.cleared_at.isoformat() if cm.cleared_at else None,
                "notes": cm.notes,
            }
            for cm in commitments
        ],
    }


async def restore_backup(db: AsyncSession, user_id: uuid.UUID, data: dict) -> dict:
    """Restore user data from a backup JSON. Clears existing data first."""
    await db.execute(Commitment.__table__.delete().where(Commitment.user_id == user_id))
    await db.execute(Transaction.__table__.delete().where(Transaction.user_id == user_id))
    await db.execute(Budget.__table__.delete().where(Budget.user_id == user_id))
    await db.execute(Category.__table__.delete().where(Category.user_id == user_id))
    await db.execute(Account.__table__.delete().where(Account.user_id == user_id))
    await db.flush()

    id_map_accounts = {}
    for a in data.get("accounts", []):
        acct = Account(
            user_id=user_id, name=a["name"],
            account_type=a["account_type"], currency=a.get("currency", "USD"),
            initial_balance=Decimal(str(a.get("initial_balance", 0))),
            current_balance=Decimal(str(a.get("initial_balance", 0))),
            institution=a.get("institution"), is_cashflow=a.get("is_cashflow", True),
            is_active=a.get("is_active", True),
            sort_order=a.get("sort_order", 0),
        )
        db.add(acct)
        await db.flush()
        id_map_accounts[a["id"]] = acct.id

    id_map_cats = {}
    parents_first = [c for c in data.get("categories", []) if not c.get("parent_id")]
    children = [c for c in data.get("categories", []) if c.get("parent_id")]

    for c in parents_first:
        cat = Category(
            user_id=user_id, name=c["name"], category_type=c["category_type"],
            sort_order=c.get("sort_order", 0),
            budgeted_amount=Decimal(str(c.get("budgeted_amount", 0))),
            reserve_amount=Decimal(str(c.get("reserve_amount", 0))),
            is_fixed=c.get("is_fixed", False),
        )
        db.add(cat)
        await db.flush()
        id_map_cats[c["id"]] = cat.id
        for kw_data in c.get("keywords", []):
            db.add(CategoryKeyword(
                category_id=cat.id, keyword=kw_data["keyword"],
                hit_count=kw_data.get("hit_count", 0),
            ))

    for c in children:
        parent_new_id = id_map_cats.get(c["parent_id"])
        cat = Category(
            user_id=user_id, name=c["name"], category_type=c["category_type"],
            parent_id=parent_new_id, sort_order=c.get("sort_order", 0),
            budgeted_amount=Decimal(str(c.get("budgeted_amount", 0))),
            reserve_amount=Decimal(str(c.get("reserve_amount", 0))),
            is_fixed=c.get("is_fixed", False),
        )
        db.add(cat)
        await db.flush()
        id_map_cats[c["id"]] = cat.id
        for kw_data in c.get("keywords", []):
            db.add(CategoryKeyword(
                category_id=cat.id, keyword=kw_data["keyword"],
                hit_count=kw_data.get("hit_count", 0),
            ))

    tx_count = 0
    for t in data.get("transactions", []):
        acct_id = id_map_accounts.get(t["account_id"])
        if not acct_id:
            continue
        cat_id = id_map_cats.get(t["category_id"]) if t.get("category_id") else None
        tx = Transaction(
            user_id=user_id, account_id=acct_id, category_id=cat_id,
            date=date.fromisoformat(t["date"]),
            amount=Decimal(str(t["amount"])),
            description=t["description"],
            original_description=t.get("original_description"),
            reference=t.get("reference"), notes=t.get("notes"),
            is_cleared=t.get("is_cleared", t.get("is_reconciled", False)),
        )
        db.add(tx)
        tx_count += 1

    for b in data.get("budgets", []):
        cat_id = id_map_cats.get(b["category_id"])
        if not cat_id:
            continue
        db.add(Budget(
            user_id=user_id, category_id=cat_id,
            year=b["year"], month=b["month"],
            amount=Decimal(str(b["amount"])),
        ))

    cm_count = 0
    for cm in data.get("commitments", []):
        cat_id = id_map_cats.get(cm["category_id"]) if cm.get("category_id") else None
        commit = Commitment(
            user_id=user_id, category_id=cat_id,
            title=cm["title"],
            amount=Decimal(str(cm["amount"])),
            direction=cm.get("direction", "outflow"),
            due_date=date.fromisoformat(cm["due_date"]),
            is_recurring=cm.get("is_recurring", False),
            recurrence=cm.get("recurrence"),
            confidence=cm.get("confidence", "confirmed"),
            is_active=cm.get("is_active", True),
            notes=cm.get("notes"),
        )
        if cm.get("cleared_at"):
            commit.cleared_at = datetime.fromisoformat(cm["cleared_at"])
        db.add(commit)
        cm_count += 1

    await db.flush()

    return {
        "accounts": len(id_map_accounts),
        "categories": len(id_map_cats),
        "transactions": tx_count,
        "budgets": len(data.get("budgets", [])),
        "commitments": cm_count,
    }


def export_table_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_table_json(rows: list[dict]) -> str:
    return json.dumps(rows, default=_serialize, indent=2)
