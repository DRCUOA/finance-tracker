"""Locked-transaction edit policy.

A transaction locked by reconciliation may change ONE field — its category —
and only via an explicit confirmation (``confirm_locked``). Every other field
is immutable, and locked rows are excluded from batch categorise/delete.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.category import Category
from app.models.transaction import Transaction
from app.routers import transactions as tx_router
from app.services import dedup


def _tx(user, account, **overrides):
    base = dict(
        user_id=user.id, account_id=account.id,
        date=date(2026, 7, 19), amount=Decimal("-10000.00"),
        description="Albany Toyota",
        reference="ref-1", notes="note-1",
        content_hash=dedup.content_hash(
            date(2026, 7, 19), Decimal("-10000.00"), "Albany Toyota"),
        occurrence=0, source="manual",
    )
    base.update(overrides)
    return Transaction(**base)


def _body(resp) -> dict:
    return json.loads(resp.body.decode())


async def _make_category(db, user, name="Vehicles"):
    cat = Category(user_id=user.id, name=name)
    db.add(cat)
    await db.flush()
    return cat


def _lock(monkeypatch, locked=True):
    monkeypatch.setattr(tx_router.tx_svc, "is_tx_locked", AsyncMock(return_value=locked))


def _modal_request(payload) -> AsyncMock:
    request = AsyncMock()
    request.json = AsyncMock(return_value=payload)
    return request


@pytest.mark.asyncio
class TestEditModalLocked:
    async def test_non_category_change_is_rejected(self, db, user, account, monkeypatch):
        tx = _tx(user, account)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        resp = await tx_router.edit_transaction_modal(
            tx_id=tx.id, request=_modal_request({"description": "tampered"}),
            user=user, db=db,
        )
        assert resp.status_code == 403
        assert "only the category" in _body(resp)["error"]
        assert tx.description == "Albany Toyota"

    async def test_full_submit_with_only_category_changed_passes(
        self, db, user, account, monkeypatch,
    ):
        """The modal sends every field; unchanged values must not trip the guard."""
        tx = _tx(user, account)
        cat = await _make_category(db, user)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        payload = {
            "date": tx.date.isoformat(),
            "amount": float(tx.amount),
            "description": tx.description,
            "account_id": str(tx.account_id),
            "reference": tx.reference,
            "notes": tx.notes,
            "category_id": str(cat.id),
            "confirm_locked": True,
        }
        resp = await tx_router.edit_transaction_modal(
            tx_id=tx.id, request=_modal_request(payload), user=user, db=db,
        )
        assert resp.status_code == 200
        assert tx.category_id == cat.id
        assert tx.description == "Albany Toyota"
        assert tx.amount == Decimal("-10000.00")

    async def test_category_change_without_confirm_is_409(
        self, db, user, account, monkeypatch,
    ):
        tx = _tx(user, account)
        cat = await _make_category(db, user)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        resp = await tx_router.edit_transaction_modal(
            tx_id=tx.id, request=_modal_request({"category_id": str(cat.id)}),
            user=user, db=db,
        )
        assert resp.status_code == 409
        assert _body(resp)["locked_confirm_required"] is True
        assert tx.category_id is None

    async def test_confirmed_category_change_applies_only_category(
        self, db, user, account, monkeypatch,
    ):
        tx = _tx(user, account)
        cat = await _make_category(db, user)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        # Hostile payload: other-field values sneak in alongside the category,
        # but match the stored row, so only the category may be written.
        payload = {
            "category_id": str(cat.id),
            "confirm_locked": True,
            "date": tx.date.isoformat(),
            "amount": float(tx.amount),
        }
        resp = await tx_router.edit_transaction_modal(
            tx_id=tx.id, request=_modal_request(payload), user=user, db=db,
        )
        assert resp.status_code == 200
        assert tx.category_id == cat.id

    async def test_noop_submit_is_ok_without_confirm(self, db, user, account, monkeypatch):
        tx = _tx(user, account)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        resp = await tx_router.edit_transaction_modal(
            tx_id=tx.id,
            request=_modal_request({"description": tx.description, "category_id": None}),
            user=user, db=db,
        )
        assert resp.status_code == 200

    async def test_unlocked_edit_still_works(self, db, user, account, monkeypatch):
        tx = _tx(user, account)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch, locked=False)

        resp = await tx_router.edit_transaction_modal(
            tx_id=tx.id, request=_modal_request({"description": "renamed"}),
            user=user, db=db,
        )
        assert resp.status_code == 200
        assert tx.description == "renamed"


@pytest.mark.asyncio
class TestInlineCategoryLocked:
    async def test_without_confirm_is_409(self, db, user, account, monkeypatch):
        tx = _tx(user, account)
        cat = await _make_category(db, user)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        resp = await tx_router.update_category_inline(
            tx_id=tx.id, request=AsyncMock(), category_id=str(cat.id),
            confirm_locked="", user=user, db=db,
        )
        assert resp.status_code == 409
        assert _body(resp)["locked_confirm_required"] is True
        assert tx.category_id is None

    async def test_with_confirm_applies(self, db, user, account, monkeypatch):
        tx = _tx(user, account)
        cat = await _make_category(db, user)
        db.add(tx)
        await db.flush()
        _lock(monkeypatch)

        resp = await tx_router.update_category_inline(
            tx_id=tx.id, request=AsyncMock(), category_id=str(cat.id),
            confirm_locked="true", user=user, db=db,
        )
        assert resp.status_code == 200
        assert tx.category_id == cat.id


@pytest.mark.asyncio
class TestBatchOpsSkipLocked:
    async def _form_request(self, tx_ids):
        request = AsyncMock()
        form = AsyncMock()
        form.getlist = lambda key: [str(t) for t in tx_ids]
        request.form = AsyncMock(return_value=form)
        request.headers = {}
        return request

    async def test_batch_categorise_skips_locked(self, db, user, account, monkeypatch):
        locked_tx = _tx(user, account)
        free_tx = _tx(
            user, account, date=date(2026, 7, 20), description="Other",
            content_hash=dedup.content_hash(
                date(2026, 7, 20), Decimal("-10000.00"), "Other"),
        )
        cat = await _make_category(db, user)
        db.add_all([locked_tx, free_tx])
        await db.flush()
        monkeypatch.setattr(
            tx_router.tx_svc, "get_locked_tx_ids",
            AsyncMock(return_value={str(locked_tx.id)}),
        )

        await tx_router.batch_categorise(
            request=await self._form_request([locked_tx.id, free_tx.id]),
            category_id=str(cat.id), user=user, db=db,
        )
        assert free_tx.category_id == cat.id
        assert locked_tx.category_id is None

    async def test_batch_delete_skips_locked(self, db, user, account, monkeypatch):
        locked_tx = _tx(user, account)
        free_tx = _tx(
            user, account, date=date(2026, 7, 20), description="Other",
            content_hash=dedup.content_hash(
                date(2026, 7, 20), Decimal("-10000.00"), "Other"),
        )
        db.add_all([locked_tx, free_tx])
        await db.flush()
        monkeypatch.setattr(
            tx_router.tx_svc, "get_locked_tx_ids",
            AsyncMock(return_value={str(locked_tx.id)}),
        )

        await tx_router.batch_delete(
            request=await self._form_request([locked_tx.id, free_tx.id]),
            user=user, db=db,
        )
        remaining = (await db.execute(
            select(Transaction.id).where(
                Transaction.id.in_([locked_tx.id, free_tx.id]))
        )).scalars().all()
        assert remaining == [locked_tx.id]
