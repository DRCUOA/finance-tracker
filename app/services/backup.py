import csv
import io
import json
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, update
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


async def export_account_bundle(
    db: AsyncSession, user_id: uuid.UUID,
    account_ids: list[uuid.UUID], include_data: bool = False,
) -> dict:
    """Export selected accounts as a portable JSON bundle.

    mode='schema': account settings only.
    mode='full': account settings + all transactions.
    """
    accounts = (await db.execute(
        select(Account)
        .where(Account.user_id == user_id, Account.id.in_(account_ids))
        .order_by(Account.sort_order)
    )).scalars().all()

    bundle: dict = {
        "version": "1.0",
        "type": "account_bundle",
        "exported_at": datetime.utcnow().isoformat(),
        "includes_data": include_data,
        "accounts": [],
    }

    for a in accounts:
        entry: dict = {
            "name": a.name,
            "account_type": a.account_type.value,
            "currency": a.currency,
            "initial_balance": float(a.initial_balance),
            "current_balance": float(a.current_balance),
            "institution": a.institution,
            "term": a.term.value,
            "is_cashflow": a.is_cashflow,
            "is_active": a.is_active,
            "sort_order": a.sort_order,
        }

        if include_data:
            txs = (await db.execute(
                select(Transaction)
                .where(Transaction.account_id == a.id)
                .options(selectinload(Transaction.category))
                .order_by(Transaction.date)
            )).scalars().all()

            entry["transactions"] = [
                {
                    "date": t.date.isoformat(),
                    "amount": float(t.amount),
                    "description": t.description,
                    "original_description": t.original_description,
                    "reference": t.reference,
                    "notes": t.notes,
                    "is_cleared": t.is_cleared,
                    "category_name": t.category.name if t.category else None,
                }
                for t in txs
            ]

        bundle["accounts"].append(entry)

    return bundle


async def import_account_bundle(
    db: AsyncSession, user_id: uuid.UUID, data: dict,
) -> dict:
    """Import accounts from an exported bundle. Skips accounts whose name
    already exists for this user."""
    from app.services.accounts import recalculate_balance

    existing = (await db.execute(
        select(Account.name).where(Account.user_id == user_id)
    )).scalars().all()
    existing_names = {n.lower() for n in existing}

    cat_cache: dict[str, uuid.UUID] = {}
    includes_data = data.get("includes_data", False)

    stats = {"imported": 0, "skipped": 0, "transactions": 0, "skipped_names": []}

    for entry in data.get("accounts", []):
        if entry["name"].lower() in existing_names:
            stats["skipped"] += 1
            stats["skipped_names"].append(entry["name"])
            continue

        acct = Account(
            user_id=user_id,
            name=entry["name"],
            account_type=entry["account_type"],
            currency=entry.get("currency", "NZD"),
            initial_balance=Decimal(str(entry.get("initial_balance", 0))),
            current_balance=Decimal(str(entry.get("current_balance",
                                                   entry.get("initial_balance", 0)))),
            institution=entry.get("institution"),
            term=entry.get("term", "short"),
            is_cashflow=entry.get("is_cashflow", True),
            is_active=entry.get("is_active", True),
            sort_order=entry.get("sort_order", 0),
        )
        db.add(acct)
        await db.flush()
        stats["imported"] += 1

        if includes_data and "transactions" in entry:
            for t in entry["transactions"]:
                cat_id = None
                cat_name = t.get("category_name")
                if cat_name:
                    if cat_name not in cat_cache:
                        row = (await db.execute(
                            select(Category.id).where(
                                Category.user_id == user_id,
                                Category.name == cat_name,
                            )
                        )).scalar_one_or_none()
                        cat_cache[cat_name] = row
                    cat_id = cat_cache[cat_name]

                tx = Transaction(
                    user_id=user_id,
                    account_id=acct.id,
                    category_id=cat_id,
                    date=date.fromisoformat(t["date"]),
                    amount=Decimal(str(t["amount"])),
                    description=t["description"],
                    original_description=t.get("original_description"),
                    reference=t.get("reference"),
                    notes=t.get("notes"),
                    is_cleared=t.get("is_cleared", False),
                )
                db.add(tx)
                stats["transactions"] += 1

            await db.flush()
            await recalculate_balance(db, acct.id)

    return stats


async def export_matching_rules(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Export all category keywords grouped by category name."""
    categories = (await db.execute(
        select(Category).where(Category.user_id == user_id)
        .options(selectinload(Category.keywords))
        .order_by(Category.name)
    )).scalars().all()

    rules = []
    for cat in categories:
        if not cat.keywords:
            continue
        rules.append({
            "category": cat.name,
            "category_type": cat.category_type.value,
            "keywords": [
                {"keyword": kw.keyword, "hit_count": kw.hit_count}
                for kw in sorted(cat.keywords, key=lambda k: k.keyword)
            ],
        })

    return {
        "version": "1.0",
        "type": "matching_rules",
        "exported_at": datetime.utcnow().isoformat(),
        "rules": rules,
    }


async def import_matching_rules(
    db: AsyncSession, user_id: uuid.UUID, data: dict,
) -> dict:
    """Import keywords from an exported rules file. Merges into existing
    categories by name; skips keywords that already exist on a category."""
    categories = (await db.execute(
        select(Category).where(Category.user_id == user_id)
        .options(selectinload(Category.keywords))
    )).scalars().all()
    cat_by_name: dict[str, Category] = {c.name.lower(): c for c in categories}

    stats = {
        "rules_processed": 0,
        "keywords_added": 0,
        "keywords_skipped": 0,
        "categories_missing": [],
    }

    for rule in data.get("rules", []):
        cat_name = rule.get("category", "")
        stats["rules_processed"] += 1

        cat = cat_by_name.get(cat_name.lower())
        if not cat:
            stats["categories_missing"].append(cat_name)
            continue

        existing_kws = {kw.keyword.lower() for kw in cat.keywords}

        for kw_data in rule.get("keywords", []):
            keyword = kw_data["keyword"]
            if keyword.lower() in existing_kws:
                stats["keywords_skipped"] += 1
                continue
            db.add(CategoryKeyword(
                category_id=cat.id,
                keyword=keyword,
                hit_count=kw_data.get("hit_count", 0),
            ))
            existing_kws.add(keyword.lower())
            stats["keywords_added"] += 1

    await db.flush()
    return stats


async def export_category_bundle(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Export all categories with their keywords as a portable JSON bundle."""
    from sqlalchemy.orm import joinedload
    categories = (await db.execute(
        select(Category).where(Category.user_id == user_id)
        .options(selectinload(Category.keywords), joinedload(Category.parent))
        .order_by(Category.sort_order)
    )).unique().scalars().all()

    cat_list = []
    for c in categories:
        cat_list.append({
            "name": c.name,
            "category_type": c.category_type.value,
            "parent_name": c.parent.name if c.parent_id else None,
            "sort_order": c.sort_order,
            "budgeted_amount": float(c.budgeted_amount),
            "reserve_amount": float(c.reserve_amount),
            "is_fixed": c.is_fixed,
            "keywords": [
                {"keyword": kw.keyword, "hit_count": kw.hit_count}
                for kw in sorted(c.keywords, key=lambda k: k.keyword)
            ],
        })

    return {
        "version": "1.0",
        "type": "category_bundle",
        "exported_at": datetime.utcnow().isoformat(),
        "categories": cat_list,
    }


async def import_category_bundle(
    db: AsyncSession, user_id: uuid.UUID, data: dict,
) -> dict:
    """Wholesale replace all categories and keywords from a bundle.

    Remaps existing transaction and commitment category_id references to the
    new categories by matching on category name. Budgets are cascade-deleted
    with the old categories.
    """
    # 1. Snapshot old category name -> id
    old_categories = (await db.execute(
        select(Category).where(Category.user_id == user_id)
    )).scalars().all()
    old_name_to_id: dict[str, uuid.UUID] = {c.name.lower(): c.id for c in old_categories}

    # 2. Build new categories in a first pass to get new IDs
    entries = data.get("categories", [])
    parents = [c for c in entries if not c.get("parent_name")]
    children = [c for c in entries if c.get("parent_name")]
    new_name_to_id: dict[str, uuid.UUID] = {}

    # Create parents
    for c in parents:
        cat = Category(
            user_id=user_id,
            name="_import_" + c["name"],
            category_type=c["category_type"],
            sort_order=c.get("sort_order", 0),
            budgeted_amount=Decimal(str(c.get("budgeted_amount", 0))),
            reserve_amount=Decimal(str(c.get("reserve_amount", 0))),
            is_fixed=c.get("is_fixed", False),
        )
        db.add(cat)
        await db.flush()
        new_name_to_id[c["name"].lower()] = cat.id
        for kw_data in c.get("keywords", []):
            db.add(CategoryKeyword(
                category_id=cat.id,
                keyword=kw_data["keyword"],
                hit_count=kw_data.get("hit_count", 0),
            ))

    # Create children
    for c in children:
        parent_id = new_name_to_id.get(c["parent_name"].lower())
        cat = Category(
            user_id=user_id,
            name="_import_" + c["name"],
            category_type=c["category_type"],
            parent_id=parent_id,
            sort_order=c.get("sort_order", 0),
            budgeted_amount=Decimal(str(c.get("budgeted_amount", 0))),
            reserve_amount=Decimal(str(c.get("reserve_amount", 0))),
            is_fixed=c.get("is_fixed", False),
        )
        db.add(cat)
        await db.flush()
        new_name_to_id[c["name"].lower()] = cat.id
        for kw_data in c.get("keywords", []):
            db.add(CategoryKeyword(
                category_id=cat.id,
                keyword=kw_data["keyword"],
                hit_count=kw_data.get("hit_count", 0),
            ))

    await db.flush()

    # 3. Remap transactions and commitments from old -> new by name match
    remapped = 0
    orphaned = 0
    for old_name, old_id in old_name_to_id.items():
        new_id = new_name_to_id.get(old_name)
        if new_id:
            r = await db.execute(
                update(Transaction)
                .where(Transaction.user_id == user_id, Transaction.category_id == old_id)
                .values(category_id=new_id)
            )
            remapped += r.rowcount
            await db.execute(
                update(Commitment)
                .where(Commitment.user_id == user_id, Commitment.category_id == old_id)
                .values(category_id=new_id)
            )
        else:
            r = await db.execute(
                update(Transaction)
                .where(Transaction.user_id == user_id, Transaction.category_id == old_id)
                .values(category_id=None)
            )
            orphaned += r.rowcount
            await db.execute(
                update(Commitment)
                .where(Commitment.user_id == user_id, Commitment.category_id == old_id)
                .values(category_id=None)
            )

    await db.flush()

    # 4. Delete old categories (budgets cascade-delete)
    # Clear parent refs first to avoid FK issues
    for cat in old_categories:
        cat.parent_id = None
    await db.flush()
    await db.execute(
        Category.__table__.delete().where(
            Category.user_id == user_id,
            Category.id.in_([c.id for c in old_categories]),
        )
    )
    await db.flush()

    # 5. Rename new categories (strip _import_ prefix)
    for c in entries:
        new_id = new_name_to_id.get(c["name"].lower())
        if new_id:
            cat_obj = await db.get(Category, new_id)
            if cat_obj:
                cat_obj.name = c["name"]
    await db.flush()

    return {
        "categories_imported": len(new_name_to_id),
        "keywords_imported": sum(len(c.get("keywords", [])) for c in entries),
        "categories_replaced": len(old_categories),
        "transactions_remapped": remapped,
        "transactions_orphaned": orphaned,
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
