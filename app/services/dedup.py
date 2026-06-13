"""Single source of truth for transaction identity and duplicate detection.

Compartment #2 (ingestion integrity). Every path that inserts a transaction —
manual create, CSV/OFX import, Akahu feed sync — decides "is this a duplicate?"
here and nowhere else. The three historical copies (``import_service``'s
``_is_duplicate`` / ``find_duplicates`` and ``transactions.check_duplicate``) are
gone.

Identity model (spec-02 §1, §8.1):

* A transaction's **content** is ``(date, amount, normalised description)``.
  ``account_id`` is the leading column of the DB constraint, not part of the
  hash. ``reference`` is deliberately **not** identity — treating a reused
  bank reference as a duplicate is exactly the silent-drop bug this fixes.
* ``content_hash`` is a stable digest of that content, shared across **all**
  sources, so a coincidental re-import (or the same payment arriving from two
  sources with identical text) collides.
* ``occurrence`` discriminates *deliberately admitted* repeats: the first row
  for a content hash is occurrence 0; an override admits the next as 1, 2, …
  and flags it ``dedup_override`` so it is auditable.

The DB constraint ``unique(account_id, content_hash, occurrence)`` is the real
guarantee; the Python checks here are the friendly first line, and callers also
catch the integrity error as the concurrency backstop.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction

_WS = re.compile(r"\s+")


def normalise_description(s: str | None) -> str:
    """Lower-case, strip, and collapse internal whitespace.

    Deterministic and source-agnostic so a CSV ``' | '``-joined description and
    the same text typed by hand hash identically.
    """
    return _WS.sub(" ", (s or "").strip().lower())


def content_hash(d: date, amount: Decimal, description: str | None) -> str:
    """Account-independent digest of a transaction's content.

    Amount keeps its sign and is quantised to 2dp so ``1.5`` and ``1.50`` agree.
    """
    amt = f"{Decimal(amount).quantize(Decimal('0.01')):f}"
    payload = f"{d.isoformat()}|{amt}|{normalise_description(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Admission:
    """The decision returned by :func:`admit`."""

    insert: bool          # False => caller skips (an accidental duplicate)
    content_hash: str
    occurrence: int       # 0 normally; >0 only via an override
    dedup_override: bool


async def find_existing(
    db: AsyncSession, account_id: uuid.UUID, ch: str,
) -> list[Transaction]:
    """All rows for this account sharing the content hash, ordered by occurrence."""
    result = await db.execute(
        select(Transaction)
        .where(Transaction.account_id == account_id, Transaction.content_hash == ch)
        .order_by(Transaction.occurrence)
    )
    return list(result.scalars())


async def is_duplicate(
    db: AsyncSession, *,
    account_id: uuid.UUID,
    d: date,
    amount: Decimal,
    description: str | None,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """True iff an occurrence-0 row with this content already exists for the account.

    ``exclude_id`` lets the edit path ignore the row being updated.
    """
    ch = content_hash(d, amount, description)
    stmt = select(Transaction.id).where(
        Transaction.account_id == account_id,
        Transaction.content_hash == ch,
        Transaction.occurrence == 0,
    )
    if exclude_id is not None:
        stmt = stmt.where(Transaction.id != exclude_id)
    result = await db.execute(stmt.limit(1))
    return result.first() is not None


async def next_occurrence(
    db: AsyncSession, account_id: uuid.UUID, ch: str,
) -> int:
    """``max(occurrence) + 1`` for this (account, content_hash); 0 if none exist."""
    result = await db.execute(
        select(func.max(Transaction.occurrence)).where(
            Transaction.account_id == account_id,
            Transaction.content_hash == ch,
        )
    )
    current = result.scalar_one_or_none()
    return 0 if current is None else current + 1


async def admit(
    db: AsyncSession, *,
    account_id: uuid.UUID,
    d: date,
    amount: Decimal,
    description: str | None,
    override: bool = False,
) -> Admission:
    """The single entry point every insert path calls.

    * No occurrence-0 row exists -> admit at occurrence 0.
    * One exists and ``override`` is False -> reject (the accidental re-import).
    * One exists and ``override`` is True -> admit at ``next_occurrence`` and
      flag ``dedup_override`` (a deliberate, audited repeat).
    """
    ch = content_hash(d, amount, description)
    exists = await is_duplicate(
        db, account_id=account_id, d=d, amount=amount, description=description,
    )
    if not exists:
        return Admission(insert=True, content_hash=ch, occurrence=0, dedup_override=False)
    if not override:
        return Admission(insert=False, content_hash=ch, occurrence=0, dedup_override=False)
    occ = await next_occurrence(db, account_id, ch)
    return Admission(insert=True, content_hash=ch, occurrence=occ, dedup_override=True)
