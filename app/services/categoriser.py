import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category, CategoryKeyword


async def suggest_category(db: AsyncSession, user_id: uuid.UUID, description: str) -> uuid.UUID | None:
    """Find the best matching category for a transaction description using keyword matching."""
    desc_lower = description.lower()

    stmt = (
        select(CategoryKeyword)
        .join(Category)
        .where(Category.user_id == user_id)
        .order_by(CategoryKeyword.hit_count.desc())
    )
    result = await db.execute(stmt)
    keywords = result.scalars().all()

    best_match = None
    best_score = -1

    for kw in keywords:
        if kw.keyword in desc_lower:
            score = len(kw.keyword) * 10 + kw.hit_count
            if score > best_score:
                best_score = score
                best_match = kw.category_id

    return best_match


async def record_categorisation(
    db: AsyncSession, user_id: uuid.UUID,
    category_id: uuid.UUID, description: str,
) -> None:
    """Increment hit_count for any matching keywords when a user confirms a categorisation."""
    desc_lower = description.lower()
    stmt = (
        select(CategoryKeyword)
        .where(CategoryKeyword.category_id == category_id)
    )
    result = await db.execute(stmt)
    for kw in result.scalars():
        if kw.keyword in desc_lower:
            kw.hit_count += 1
    await db.flush()


async def extract_keywords(description: str) -> list[str]:
    """Extract significant words from a description for potential keyword creation."""
    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "it", "as", "was", "are", "be",
        "has", "had", "have", "do", "does", "did", "will", "can", "may",
        "payment", "purchase", "transaction", "debit", "credit", "pos",
    }
    words = description.lower().split()
    return [w.strip(".,;:!?#*()-") for w in words if len(w) > 2 and w.lower().strip(".,;:!?#*()-") not in stop_words]
