import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.category import Category, CategoryKeyword
from app.models.transaction import Transaction
from app.services.categoriser import _SHORT_KW_THRESHOLD, _keyword_matches
from app.services.transactions import get_locked_tx_ids

KNOWN_LOCATION_WORDS = frozenset({
    "albany", "wellington", "auckland", "christchurch", "hamilton",
    "tauranga", "dunedin", "napier", "nelson", "rotorua",
    "clark", "central", "new zealand", "north", "south", "east", "west",
})


async def list_rules(db: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """All category keywords as flat rows for the Matching Rules UI."""
    Parent = aliased(Category)
    stmt = (
        select(CategoryKeyword, Category, Parent)
        .join(Category, CategoryKeyword.category_id == Category.id)
        .outerjoin(Parent, Category.parent_id == Parent.id)
        .where(Category.user_id == user_id)
    )
    result = await db.execute(stmt)
    rows: list[dict[str, Any]] = []
    for kw, cat, parent in result.all():
        rows.append({
            "keyword_id": kw.id,
            "keyword": kw.keyword,
            "hit_count": kw.hit_count,
            "category_id": cat.id,
            "category_name": cat.name,
            "parent_name": parent.name if parent else None,
            "parent_sort": parent.sort_order if parent else -1,
            "child_sort": cat.sort_order,
        })
    rows.sort(key=lambda r: (r["parent_sort"], r["child_sort"], r["keyword"]))
    return rows


def _uncategorised_matching_stmt(user_id: uuid.UUID, phrase: str):
    """Uncategorised rows whose description contains phrase, as a SELECT.

    Substring-only: short phrases still need the word-boundary refinement the
    categoriser applies, so callers pass the results through
    ``_keyword_matches``.
    """
    return select(Transaction).where(
        Transaction.user_id == user_id,
        Transaction.category_id.is_(None),
        Transaction.description.ilike(f"%{phrase}%"),
    )


async def count_uncategorized_matching(
    db: AsyncSession, user_id: uuid.UUID, phrase: str,
) -> int:
    """How many uncategorised transactions the categoriser would match.

    Mirrors ``categoriser._keyword_matches``: phrases of four characters or
    fewer only match on word boundaries, so the preview can't promise hits the
    engine will refuse to make.
    """
    phrase = phrase.strip().lower()
    if not phrase:
        return 0

    if len(phrase) > _SHORT_KW_THRESHOLD:
        stmt = select(sa_func.count()).select_from(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.category_id.is_(None),
            Transaction.description.ilike(f"%{phrase}%"),
        )
        return (await db.execute(stmt)).scalar() or 0

    result = await db.execute(
        _uncategorised_matching_stmt(user_id, phrase).with_only_columns(
            Transaction.description,
        )
    )
    return sum(
        1 for (desc,) in result.all() if _keyword_matches(phrase, desc.lower())
    )


async def apply_rule_to_uncategorised(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID, phrase: str,
) -> tuple[int, int]:
    """Assign category_id to uncategorised transactions matching phrase.

    Returns (applied, skipped_locked). Reconciliation-locked rows are left
    alone — their category may only change through the explicit
    confirm-locked path. Any category type is a valid target, matching what
    the categoriser will do on the next import or sync.

    Only ever fills a blank category; an existing categorisation is never
    overwritten.
    """
    phrase = phrase.strip().lower()
    if not phrase:
        return 0, 0

    cat = await db.get(Category, category_id)
    if not cat or cat.user_id != user_id:
        return 0, 0

    result = await db.execute(_uncategorised_matching_stmt(user_id, phrase))
    candidates = [
        tx for tx in result.scalars().all()
        if _keyword_matches(phrase, tx.description.lower())
    ]
    if not candidates:
        return 0, 0

    locked = await get_locked_tx_ids(db, [tx.id for tx in candidates])
    applied = 0
    for tx in candidates:
        if str(tx.id) in locked:
            continue
        tx.category_id = category_id
        applied += 1

    await db.flush()
    return applied, len(candidates) - applied


async def keyword_health_report(
    db: AsyncSession, user_id: uuid.UUID,
) -> dict[str, list[dict[str, Any]]]:
    """Analyse keyword quality and return issues grouped by type."""
    Parent = aliased(Category)
    stmt = (
        select(CategoryKeyword, Category.name.label("cat_name"), Parent.name.label("parent_name"))
        .join(Category, CategoryKeyword.category_id == Category.id)
        .outerjoin(Parent, Category.parent_id == Parent.id)
        .where(Category.user_id == user_id)
        .order_by(CategoryKeyword.keyword)
    )
    result = await db.execute(stmt)
    all_rows = result.all()

    # Build lookup: keyword text -> list of (kw obj, cat_name, parent_name)
    kw_map: dict[str, list[tuple]] = {}
    for kw, cat_name, parent_name in all_rows:
        kw_map.setdefault(kw.keyword, []).append((kw, cat_name, parent_name))

    duplicates: list[dict[str, Any]] = []
    zero_hit: list[dict[str, Any]] = []
    short_broad: list[dict[str, Any]] = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    for keyword_text, entries in kw_map.items():
        # Duplicates: same keyword in multiple categories
        if len(entries) > 1:
            duplicates.append({
                "keyword": keyword_text,
                "categories": [
                    {
                        "keyword_id": str(kw.id),
                        "category_name": cat_name,
                        "parent_name": parent_name,
                        "hit_count": kw.hit_count,
                    }
                    for kw, cat_name, parent_name in entries
                ],
            })

        for kw, cat_name, parent_name in entries:
            row_info = {
                "keyword_id": str(kw.id),
                "keyword": kw.keyword,
                "category_name": cat_name,
                "parent_name": parent_name,
                "hit_count": kw.hit_count,
            }

            # Zero-hit stale
            if kw.hit_count == 0 and kw.created_at and kw.created_at < cutoff:
                zero_hit.append(row_info)

            # Short or broad/location-based
            if len(kw.keyword) <= _SHORT_KW_THRESHOLD or kw.keyword in KNOWN_LOCATION_WORDS:
                reason = []
                if len(kw.keyword) <= _SHORT_KW_THRESHOLD:
                    reason.append("very short — word-boundary matching only")
                if kw.keyword in KNOWN_LOCATION_WORDS:
                    reason.append("location word")
                short_broad.append({**row_info, "reason": ", ".join(reason)})

    return {
        "duplicates": sorted(duplicates, key=lambda d: d["keyword"]),
        "zero_hit": sorted(zero_hit, key=lambda d: d["keyword"]),
        "short_broad": sorted(short_broad, key=lambda d: d["keyword"]),
    }
