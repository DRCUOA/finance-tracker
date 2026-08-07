import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category, CategoryKeyword
from app.models.transaction import Transaction

_SHORT_KW_THRESHOLD = 4


@lru_cache(maxsize=1024)
def _word_boundary_pattern(keyword: str) -> re.Pattern:
    return re.compile(r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])")


def _keyword_matches(keyword: str, description: str) -> bool:
    """Check whether *keyword* matches *description* (both already lowercase).

    Short keywords (<=4 chars) require word-boundary matching so that e.g.
    "on" doesn't match inside "processed on:" by accident of being embedded
    in "loan".  Longer keywords use plain substring matching.
    """
    if len(keyword) <= _SHORT_KW_THRESHOLD:
        return bool(_word_boundary_pattern(keyword).search(description))
    return keyword in description


async def _load_keywords(
    db: AsyncSession, user_id: uuid.UUID,
) -> Sequence[CategoryKeyword]:
    """Every keyword the user has authored, whatever its category type.

    Transfer and non-cash categories are matchable: a statement line reading
    "Online Payment - Thank You" *is* a transfer, and the alternative to
    matching it is leaving it uncategorised, which leaks it into the spending
    pulse's uncategorised bucket. Cash-basis reports exclude these rows by
    category type, so how the category got assigned doesn't affect them.
    A keyword on such a category is an explicit statement of intent.
    """
    stmt = (
        select(CategoryKeyword)
        .join(Category)
        .where(Category.user_id == user_id)
        .order_by(CategoryKeyword.hit_count.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


def _best_match(
    keywords: Sequence[CategoryKeyword], description: str,
) -> CategoryKeyword | None:
    """Highest-scoring keyword, or None. Longer keywords win ties.

    Returns the keyword itself rather than its category so callers can record
    the hit against the rule that actually won — see ``record_match``.
    """
    desc_lower = description.lower()
    best_match = None
    best_score = -1
    for kw in keywords:
        if _keyword_matches(kw.keyword, desc_lower):
            score = len(kw.keyword) * 10 + kw.hit_count
            if score > best_score:
                best_score = score
                best_match = kw
    return best_match


def record_match(keyword: CategoryKeyword | None, times: int = 1) -> uuid.UUID | None:
    """Count a keyword's automatic matches and return the category to file under.

    Every path that lets a keyword categorise something goes through here, so
    the Hits column means "transactions this rule has caught" rather than
    "times someone confirmed it by hand" — which read as zero for rules that
    were quietly working, and had the keyword health report recommending them
    for deletion.
    """
    if keyword is None or times <= 0:
        return None
    keyword.hit_count += times
    return keyword.category_id


async def build_suggester(
    db: AsyncSession, user_id: uuid.UUID,
) -> Callable[[str], CategoryKeyword | None]:
    """Load keywords once, return a matcher to reuse across many descriptions.

    Returns the matched keyword — pass it to ``record_match`` to count the hit
    and get the category id. Use on bulk paths (statement import) to avoid a
    query per description.
    """
    keywords = await _load_keywords(db, user_id)
    return lambda description: _best_match(keywords, description)


@dataclass
class SuggestedMatch:
    transaction: Transaction
    category: Category
    matched_keyword: str
    score: int


async def batch_suggest_categories(
    db: AsyncSession, user_id: uuid.UUID,
) -> tuple[list[SuggestedMatch], int]:
    """Match all uncategorized transactions against current keywords.

    Returns (matches, total_uncategorized) where matches only includes
    transactions that found a keyword hit.
    """
    kw_stmt = (
        select(CategoryKeyword)
        .join(Category)
        .where(Category.user_id == user_id)
        .options(selectinload(CategoryKeyword.category))
        .order_by(CategoryKeyword.hit_count.desc())
    )
    kw_result = await db.execute(kw_stmt)
    keywords = kw_result.scalars().all()

    tx_stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id, Transaction.category_id.is_(None))
        .options(selectinload(Transaction.account))
        .order_by(Transaction.date.desc())
    )
    tx_result = await db.execute(tx_stmt)
    uncategorized = tx_result.scalars().all()

    matches: list[SuggestedMatch] = []
    for tx in uncategorized:
        desc_lower = tx.description.lower()
        best_kw = None
        best_score = -1
        best_cat = None

        for kw in keywords:
            if _keyword_matches(kw.keyword, desc_lower):
                score = len(kw.keyword) * 10 + kw.hit_count
                if score > best_score:
                    best_score = score
                    best_kw = kw.keyword
                    best_cat = kw.category

        if best_cat and best_kw:
            matches.append(SuggestedMatch(
                transaction=tx,
                category=best_cat,
                matched_keyword=best_kw,
                score=best_score,
            ))

    return matches, len(uncategorized)


async def suggest_category(db: AsyncSession, user_id: uuid.UUID, description: str) -> uuid.UUID | None:
    """Find the best matching category for a transaction description using keyword matching.

    Every category type is matchable, transfer and non-cash included — see
    ``_load_keywords``. Short keywords (<=4 chars) require word-boundary
    matches to avoid false positives like "on" matching inside "loan".

    Suggestion only — nothing is counted. Use ``suggest_and_record`` where the
    result is actually written to a transaction.
    """
    kw = _best_match(await _load_keywords(db, user_id), description)
    return kw.category_id if kw else None


async def suggest_and_record(
    db: AsyncSession, user_id: uuid.UUID, description: str,
) -> uuid.UUID | None:
    """``suggest_category``, counting the hit against the winning keyword."""
    return record_match(_best_match(await _load_keywords(db, user_id), description))


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
