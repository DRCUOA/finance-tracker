"""Compartment #2 — cross-source ingestion-integrity tests (spec-02 §5).

One content identity, one dedup routine. These tests pin the locked behaviour:

  * identity is ``(account_id, date, amount, normalised description)`` — NOT
    reference — backed by ``uq_transactions_account_content_occurrence``;
  * a re-import of identical content is rejected (idempotent), while two
    distinct payments that merely share a bank reference both insert (the
    silent-drop bug fix);
  * a deliberate repeat is admitted via the override path at the next
    ``occurrence`` and flagged ``dedup_override`` (auditable);
  * the Akahu feed never overrides — it adopts a content-matching non-feed row
    instead of double-counting;
  * the DB unique constraint is the concurrency backstop;
  * route-level edits surface the collision as a 409 / redirect.
"""
from __future__ import annotations

import subprocess
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType, AccountTerm
from app.models.transaction import Transaction
from app.routers import transactions as tx_router
from app.services import akahu as akahu_svc
from app.services import dedup
from app.services import transactions as tx_svc
from app.services.transactions import DuplicateTransactionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def second_account(db: AsyncSession, user) -> Account:
    """A second account on the same user, for cross-account checks."""
    a = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Second account",
        account_type=AccountType.CHECKING,
        currency="NZD",
        initial_balance=Decimal("0.00"),
        institution=None,
        term=AccountTerm.SHORT,
    )
    db.add(a)
    await db.flush()
    return a


# ---------------------------------------------------------------------------
# 1. Idempotent re-import (same content twice → one row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIdempotentReimport:
    async def test_identical_content_second_create_rejected(self, db, user, account):
        await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-12.34"), "Coffee", reference="A",
        )
        # Same content, *different* reference — still a duplicate.
        with pytest.raises(DuplicateTransactionError):
            await tx_svc.create_transaction(
                db, user.id, account.id,
                date(2026, 4, 18), Decimal("-12.34"), "Coffee", reference="B",
            )
        rows = (await db.execute(
            select(Transaction).where(Transaction.account_id == account.id)
        )).scalars().all()
        assert len(rows) == 1

    async def test_create_sets_content_hash_and_occurrence(self, db, user, account):
        tx = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-12.34"), "Coffee",
        )
        assert tx.source == "manual"
        assert tx.occurrence == 0
        assert tx.dedup_override is False
        assert tx.content_hash == dedup.content_hash(
            date(2026, 4, 18), Decimal("-12.34"), "Coffee"
        )

    async def test_whitespace_and_case_insensitive_match(self, db, user, account):
        await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-12.34"), "Coffee Shop",
        )
        with pytest.raises(DuplicateTransactionError):
            await tx_svc.create_transaction(
                db, user.id, account.id,
                date(2026, 4, 18), Decimal("-12.34"), "  coffee   shop ",
            )


# ---------------------------------------------------------------------------
# 2. Reused reference, distinct payment → both insert (the bug fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReusedReferenceDistinctPayment:
    async def test_same_reference_distinct_amount_both_insert(self, db, user, account):
        await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-1.00"), "Payment one", reference="REF-001",
        )
        # Reused reference but a genuinely distinct payment — must NOT be dropped.
        await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-2.00"), "Payment two", reference="REF-001",
        )
        rows = (await db.execute(
            select(Transaction).where(Transaction.reference == "REF-001")
        )).scalars().all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# 3. Override path (deliberate repeat → occurrence 1 + dedup_override)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOverridePath:
    async def test_override_admits_at_next_occurrence_and_flags(self, db, user, account):
        first = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-5.00"), "Lunch",
        )
        assert (first.occurrence, first.dedup_override) == (0, False)

        # Without override: rejected.
        with pytest.raises(DuplicateTransactionError):
            await tx_svc.create_transaction(
                db, user.id, account.id,
                date(2026, 4, 18), Decimal("-5.00"), "Lunch",
            )

        # With override: admitted, distinct, audited.
        second = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-5.00"), "Lunch", force=True,
        )
        assert second.occurrence == 1
        assert second.dedup_override is True
        assert second.content_hash == first.content_hash

        rows = (await db.execute(
            select(Transaction).where(Transaction.content_hash == first.content_hash)
        )).scalars().all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# 4. Concurrency backstop — the DB constraint, not the Python check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConcurrencyBackstop:
    async def test_two_raw_inserts_same_content_occurrence_violate(self, db, user, account):
        ch = dedup.content_hash(date(2026, 4, 18), Decimal("-3.00"), "Race")
        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-3.00"), description="Race",
            content_hash=ch, occurrence=0,
        ))
        await db.flush()
        # A second row at the same (account, content_hash, occurrence) — the
        # exact race two Python checks could both pass — must be rejected by
        # the unique constraint.
        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-3.00"), description="Race",
            content_hash=ch, occurrence=0,
        ))
        with pytest.raises(IntegrityError):
            await db.flush()


# ---------------------------------------------------------------------------
# 5. Feed never overrides — Akahu adopts a content-matching manual row
# ---------------------------------------------------------------------------


def _raw_akahu_tx(*, _id: str, account_akahu_id: str, amount: str, description: str):
    """Minimal raw Akahu payload. Date in UTC resolves to 2026-04-18 NZ."""
    return {
        "_id": _id,
        "_account": account_akahu_id,
        "date": "2026-04-18T00:00:00Z",
        "amount": amount,
        "description": description,
        "updated_at": "2026-04-18T01:00:00Z",
        "meta": {"reference": "bank-ref"},
    }


@pytest.mark.asyncio
class TestFeedAdoption:
    async def test_find_adoptable_returns_matching_non_feed_row(self, db, user, account):
        manual = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-12.34"), "Coffee",
        )
        ch = manual.content_hash
        found = await akahu_svc._find_adoptable(db, account.id, ch)
        assert found is not None and found.id == manual.id

    async def test_find_adoptable_skips_existing_feed_row(self, db, user, account):
        ch = dedup.content_hash(date(2026, 4, 18), Decimal("-12.34"), "Coffee")
        db.add(Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-12.34"), description="Coffee",
            content_hash=ch, occurrence=0,
            source="akahu", akahu_transaction_id="tx_already",
        ))
        await db.flush()
        assert await akahu_svc._find_adoptable(db, account.id, ch) is None

    async def test_sync_adopts_manual_row_instead_of_inserting(
        self, db, user, account, monkeypatch,
    ):
        manual = await tx_svc.create_transaction(
            db, user.id, account.id,
            date(2026, 4, 18), Decimal("-12.34"), "Coffee",
        )

        async def fake_fetch(akahu_account_id, start_utc, end_utc):
            return [_raw_akahu_tx(
                _id="tx_feed_1", account_akahu_id=account.akahu_id,
                amount="-12.34", description="Coffee",
            )]

        monkeypatch.setattr(akahu_svc, "fetch_account_transactions", fake_fetch)
        monkeypatch.setattr(
            akahu_svc, "suggest_and_record", AsyncMock(return_value=None),
        )

        result = await akahu_svc.sync_account_transactions(
            db, user.id, account.id,
            "2026-04-17T00:00:00Z", "2026-04-19T00:00:00Z",
        )

        # No second row: the manual row was adopted, not duplicated.
        rows = (await db.execute(
            select(Transaction).where(Transaction.account_id == account.id)
        )).scalars().all()
        assert len(rows) == 1
        assert result["inserted"] == 0

        await db.refresh(manual)
        assert manual.source == "akahu"
        assert manual.akahu_transaction_id == "tx_feed_1"


# ---------------------------------------------------------------------------
# 6. Migration backfill algorithm + hash-drift guard (spec-02 §4)
# ---------------------------------------------------------------------------


class TestBackfillAlgorithm:
    def test_hash_is_stable_across_calls(self):
        """Re-hashing the same content twice agrees (no runtime drift)."""
        a = dedup.content_hash(date(2026, 4, 18), Decimal("-12.34"), "Coffee | Shop")
        b = dedup.content_hash(date(2026, 4, 18), Decimal("-12.340"), " coffee | shop ")
        assert a == b

    def test_ascending_occurrence_and_override_on_repeats(self):
        """Mirror migration 022's defaultdict backfill over a tiny fixture."""
        acct = uuid.uuid4()
        rows = [
            (date(2026, 4, 18), Decimal("-5.00"), "Lunch"),   # occ 0
            (date(2026, 4, 18), Decimal("-5.00"), "Lunch"),   # occ 1 (override)
            (date(2026, 4, 18), Decimal("-5.00"), "Lunch"),   # occ 2 (override)
            (date(2026, 4, 18), Decimal("-9.00"), "Petrol"),  # occ 0
        ]
        seen: dict[tuple, int] = defaultdict(int)
        out = []
        for d, amt, desc in rows:
            ch = dedup.content_hash(d, amt, desc)
            occ = seen[(acct, ch)]
            seen[(acct, ch)] += 1
            out.append((occ, occ > 0))

        assert [o for o, _ in out] == [0, 1, 2, 0]
        assert [ovr for _, ovr in out] == [False, True, True, False]


# ---------------------------------------------------------------------------
# 7. Single-rule guard — the 3 copies are gone
# ---------------------------------------------------------------------------


class TestSingleRuleGuard:
    def test_no_legacy_dedup_helpers_outside_dedup_module(self):
        app_dir = Path(__file__).resolve().parent.parent / "app"
        hits = subprocess.run(
            ["grep", "-rln", "--include=*.py", r"_is_duplicate\|check_duplicate", str(app_dir)],
            capture_output=True, text=True,
        ).stdout.split()
        # Only the dedup module may mention the old names (in its docstring).
        offenders = [h for h in hits if not h.endswith("services/dedup.py")]
        assert offenders == [], f"legacy dedup copies still present: {offenders}"


# ---------------------------------------------------------------------------
# 8. Route-level error handling for the content dedupe constraint
# ---------------------------------------------------------------------------


def _make_integrity_error(message: str) -> IntegrityError:
    """Build a standalone IntegrityError whose ``.orig`` stringifies to message."""
    return IntegrityError(statement="UPDATE transactions ...", params=None, orig=Exception(message))


class TestIsDedupeViolation:
    def test_identifies_content_constraint_by_name(self):
        exc = _make_integrity_error(
            'duplicate key value violates unique constraint '
            '"uq_transactions_account_content_occurrence"'
        )
        assert tx_router._is_dedupe_violation(exc) is True

    def test_does_not_match_unrelated_constraint(self):
        exc = _make_integrity_error(
            'null value in column "user_id" violates not-null constraint'
        )
        assert tx_router._is_dedupe_violation(exc) is False


@pytest.mark.asyncio
class TestModalReturns409OnDuplicate:
    async def test_python_precheck_returns_409(self, db, user, account, monkeypatch):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", content_hash=dedup.content_hash(
                date(2026, 4, 18), Decimal("-1.00"), "x"),
            occurrence=0, source="manual",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise DuplicateTransactionError("dupe")

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"description": "y"})

        response = await tx_router.edit_transaction_modal(
            tx_id=existing.id, request=request, user=user, db=db,
        )
        assert response.status_code == 409
        assert "same date, amount and description" in response.body.decode()

    async def test_constraint_backstop_returns_409(self, db, user, account, monkeypatch):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", content_hash="h", occurrence=0, source="manual",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise _make_integrity_error(
                'duplicate key value violates unique constraint '
                '"uq_transactions_account_content_occurrence"'
            )

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"description": "y"})

        response = await tx_router.edit_transaction_modal(
            tx_id=existing.id, request=request, user=user, db=db,
        )
        assert response.status_code == 409

    async def test_unrelated_integrity_error_is_not_swallowed(
        self, db, user, account, monkeypatch,
    ):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", content_hash="h", occurrence=0, source="manual",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise _make_integrity_error("some other constraint blew up")

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"description": "new"})

        with pytest.raises(IntegrityError):
            await tx_router.edit_transaction_modal(
                tx_id=existing.id, request=request, user=user, db=db,
            )


@pytest.mark.asyncio
class TestFormRedirectsOnDuplicate:
    async def test_redirects_with_error_query_param(self, db, user, account, monkeypatch):
        existing = Transaction(
            user_id=user.id, account_id=account.id,
            date=date(2026, 4, 18), amount=Decimal("-1.00"),
            description="x", content_hash="h", occurrence=0, source="manual",
        )
        db.add(existing)
        await db.flush()

        async def boom(*args, **kwargs):
            raise DuplicateTransactionError("dupe")

        monkeypatch.setattr(tx_svc, "update_transaction", boom)
        monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=False))
        monkeypatch.setattr(
            tx_router.tx_svc, "get_transaction", AsyncMock(return_value=existing),
        )

        request = AsyncMock()
        response = await tx_router.update_transaction(
            tx_id=existing.id, request=request,
            account_id=str(account.id), tx_date="2026-04-18",
            amount="-1.00", description="x", category_id="",
            reference="r", notes="",
            user=user, db=db,
        )
        assert response.status_code == 302
        assert "error=duplicate" in response.headers["location"]
