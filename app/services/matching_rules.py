import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sa_func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.category import Category, CategoryKeyword
from app.models.transaction import Transaction
from app.services.categoriser import _SHORT_KW_THRESHOLD

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


async def count_uncategorized_matching(
    db: AsyncSession, user_id: uuid.UUID, phrase: str,
) -> int:
    """How many uncategorised transactions contain phrase (case-insensitive)."""
    phrase = phrase.strip()
    if not phrase:
        return 0
    pattern = f"%{phrase}%"
    stmt = select(sa_func.count()).select_from(Transaction).where(
        Transaction.user_id == user_id,
        Transaction.category_id.is_(None),
        Transaction.description.ilike(pattern),
    )
    return (await db.execute(stmt)).scalar() or 0


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
