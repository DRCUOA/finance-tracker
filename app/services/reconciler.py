import re
import uuid
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import CategoryKeyword, Category
from app.models.statement import MatchType, StatementLine
from app.models.transaction import Transaction


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


async def auto_match_statement(
    db: AsyncSession, user_id: uuid.UUID, statement_id: uuid.UUID,
) -> dict:
    """Run three-pass matching on a statement's lines against existing transactions."""
    stmt_q = select(StatementLine).where(
        StatementLine.statement_id == statement_id,
        StatementLine.match_type == MatchType.NONE,
    )
    result = await db.execute(stmt_q)
    lines = list(result.scalars().all())

    stats = {"exact": 0, "keyword": 0, "fuzzy": 0, "unmatched": 0}

    for line in lines:
        tx_q = select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.date == line.date,
            Transaction.amount == line.amount,
            Transaction.is_reconciled.is_(False),
        )
        txs = (await db.execute(tx_q)).scalars().all()

        # Pass 1: Exact match
        matched = False
        norm_desc = _normalise(line.description)
        for tx in txs:
            if _normalise(tx.description) == norm_desc:
                line.matched_transaction_id = tx.id
                line.match_type = MatchType.EXACT
                line.match_confidence = 1.0
                tx.statement_line_id = line.id
                tx.is_reconciled = True
                stats["exact"] += 1
                matched = True
                break

        if matched:
            continue

        # Pass 2: Keyword overlap
        for tx in txs:
            words_line = set(norm_desc.split())
            words_tx = set(_normalise(tx.description).split())
            if words_line and words_tx:
                overlap = len(words_line & words_tx) / max(len(words_line | words_tx), 1)
                if overlap >= 0.5:
                    line.matched_transaction_id = tx.id
                    line.match_type = MatchType.KEYWORD
                    line.match_confidence = round(0.5 + overlap * 0.4, 2)
                    stats["keyword"] += 1
                    matched = True
                    break

        if matched:
            continue

        # Pass 3: Fuzzy match (expand date range)
        fuzzy_q = select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.amount == line.amount,
            Transaction.date.between(line.date - timedelta(days=2), line.date + timedelta(days=2)),
            Transaction.is_reconciled.is_(False),
        )
        fuzzy_txs = (await db.execute(fuzzy_q)).scalars().all()

        best_score = 0.0
        best_tx = None
        for tx in fuzzy_txs:
            score = fuzz.token_sort_ratio(_normalise(line.description), _normalise(tx.description)) / 100.0
            if score > best_score and score >= 0.4:
                best_score = score
                best_tx = tx

        if best_tx:
            line.matched_transaction_id = best_tx.id
            line.match_type = MatchType.FUZZY
            line.match_confidence = round(best_score * 0.8, 2)
            stats["fuzzy"] += 1
        else:
            stats["unmatched"] += 1

    await db.flush()
    return stats


async def confirm_match(
    db: AsyncSession, line_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    line = await db.get(StatementLine, line_id)
    if not line or not line.matched_transaction_id:
        return False
    tx = await db.get(Transaction, line.matched_transaction_id)
    if not tx or tx.user_id != user_id:
        return False
    tx.statement_line_id = line.id
    tx.is_reconciled = True
    await db.flush()
    return True


async def reject_match(db: AsyncSession, line_id: uuid.UUID) -> bool:
    line = await db.get(StatementLine, line_id)
    if not line:
        return False
    line.matched_transaction_id = None
    line.match_type = MatchType.NONE
    line.match_confidence = 0.0
    await db.flush()
    return True


async def manual_match(
    db: AsyncSession, line_id: uuid.UUID, tx_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    line = await db.get(StatementLine, line_id)
    tx = await db.get(Transaction, tx_id)
    if not line or not tx or tx.user_id != user_id:
        return False
    line.matched_transaction_id = tx.id
    line.match_type = MatchType.MANUAL
    line.match_confidence = 1.0
    tx.statement_line_id = line.id
    tx.is_reconciled = True
    await db.flush()
    return True
