"""Matching rules must reach every account, whatever fed it.

Rules used to apply only where the categoriser was actually called — manual
entry and the Akahu feed — so an account fed by CSV/OFX import never
auto-categorised and the feature looked broken for that account. The contract
under test:

* statement import runs the same matching rules as the other ingest paths;
* saving a rule applies it to transactions already sitting uncategorised,
  without touching reconciliation-locked or already-categorised rows;
* every category type is a valid rule target, transfer and non-cash included
  — a statement line reading "Online Payment - Thank You" is a transfer, and
  cash-basis reports exclude those rows by category type regardless of how
  they were categorised;
* the preview count uses the engine's word-boundary rule for short phrases,
  so it can't promise matches that will never happen.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.category import Category, CategoryKeyword, CategoryType
from app.models.statement import FileType, Statement, StatementLine, StatementStatus
from app.models.transaction import Transaction
from app.routers import matching_rules as mr_router
from app.services import categoriser
from app.services import dedup
from app.services import import_service as import_svc
from app.services import matching_rules as mr_svc


async def _category(db, user, name="Groceries", ctype=CategoryType.EXPENSE) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user.id, name=name,
        category_type=ctype, sort_order=0,
    )
    db.add(cat)
    await db.flush()
    return cat


async def _keyword(db, category, text="countdown") -> CategoryKeyword:
    kw = CategoryKeyword(category_id=category.id, keyword=text)
    db.add(kw)
    await db.flush()
    return kw


async def _statement(db, user, account, descriptions) -> Statement:
    stmt = Statement(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        filename="anz.csv", file_type=FileType.CSV,
        record_count=len(descriptions), status=StatementStatus.PENDING,
    )
    db.add(stmt)
    await db.flush()
    lines = []
    for i, desc in enumerate(descriptions):
        line = StatementLine(
            id=uuid.uuid4(), statement_id=stmt.id,
            date=date(2026, 7, 1 + i), amount=Decimal("-25.00"),
            description=desc, reference=None,
        )
        db.add(line)
        lines.append(line)
    await db.flush()
    return stmt, lines


async def _uncategorised_tx(db, user, account, description, **overrides) -> Transaction:
    base = dict(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        date=date(2026, 7, 4), amount=Decimal("-19.90"),
        description=description, original_description=description,
        content_hash=dedup.content_hash(
            date(2026, 7, 4), Decimal("-19.90"), description),
        occurrence=0, source="import",
    )
    base.update(overrides)
    tx = Transaction(**base)
    db.add(tx)
    await db.flush()
    return tx


class TestImportAppliesRules:
    """CSV/OFX import is an ingest path like any other."""

    @pytest.mark.asyncio
    async def test_imported_lines_are_categorised(self, db, user, unlinked_account):
        cat = await _category(db, user)
        await _keyword(db, cat, "countdown")
        stmt, lines = await _statement(
            db, user, unlinked_account,
            ["COUNTDOWN ALBANY", "Hollywood Bakery Auckland Nz"],
        )

        result = await import_svc.import_statement_lines(
            db, user.id, stmt.id, [ln.id for ln in lines], unlinked_account.id,
        )

        assert result.imported == 2
        assert result.categorised == 1

    @pytest.mark.asyncio
    async def test_matched_line_gets_the_keyword_category(self, db, user, unlinked_account):
        cat = await _category(db, user)
        await _keyword(db, cat, "countdown")
        stmt, lines = await _statement(
            db, user, unlinked_account,
            ["COUNTDOWN ALBANY", "Hollywood Bakery Auckland Nz"],
        )

        await import_svc.import_statement_lines(
            db, user.id, stmt.id, [ln.id for ln in lines], unlinked_account.id,
        )


        rows = (await db.execute(select(Transaction))).scalars().all()
        by_desc = {tx.description: tx.category_id for tx in rows}
        assert by_desc["COUNTDOWN ALBANY"] == cat.id
        assert by_desc["Hollywood Bakery Auckland Nz"] is None

    @pytest.mark.asyncio
    async def test_transfer_keyword_matches_on_import(self, db, user, unlinked_account):
        transfer = await _category(db, user, "Card payment", CategoryType.TRANSFER)
        await _keyword(db, transfer, "online payment - thank you")
        stmt, lines = await _statement(
            db, user, unlinked_account, ["Online Payment - Thank You"],
        )

        result = await import_svc.import_statement_lines(
            db, user.id, stmt.id, [ln.id for ln in lines], unlinked_account.id,
        )

        assert (result.imported, result.categorised) == (1, 1)
        tx = (await db.execute(select(Transaction))).scalars().one()
        assert tx.category_id == transfer.id


class TestBackfillOnSave:
    """Saving a rule is not inert — it works on what is already there."""

    @pytest.mark.asyncio
    async def test_applies_to_existing_uncategorised(self, db, user, unlinked_account):
        cat = await _category(db, user)
        tx = await _uncategorised_tx(db, user, unlinked_account, "COUNTDOWN ALBANY")

        applied, locked = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, cat.id, "countdown",
        )

        assert (applied, locked) == (1, 0)
        assert tx.category_id == cat.id

    @pytest.mark.asyncio
    async def test_never_overwrites_an_existing_category(self, db, user, unlinked_account):
        cat = await _category(db, user)
        other = await _category(db, user, "Dining")
        tx = await _uncategorised_tx(
            db, user, unlinked_account, "COUNTDOWN ALBANY", category_id=other.id,
        )

        applied, _ = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, cat.id, "countdown",
        )

        assert applied == 0
        assert tx.category_id == other.id

    @pytest.mark.asyncio
    async def test_leaves_reconciliation_locked_rows_alone(self, db, user, unlinked_account):
        cat = await _category(db, user)
        tx = await _uncategorised_tx(
            db, user, unlinked_account, "COUNTDOWN ALBANY", is_cleared=True,
        )

        applied, locked = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, cat.id, "countdown",
        )

        assert (applied, locked) == (0, 1)
        assert tx.category_id is None

    @pytest.mark.asyncio
    async def test_short_phrase_respects_word_boundaries(self, db, user, unlinked_account):
        cat = await _category(db, user)
        embedded = await _uncategorised_tx(db, user, unlinked_account, "Loan repayment")
        standalone = await _uncategorised_tx(
            db, user, unlinked_account, "Processed on: 4 July",
        )

        applied, _ = await mr_svc.apply_rule_to_uncategorised(db, user.id, cat.id, "on")

        assert applied == 1
        assert embedded.category_id is None
        assert standalone.category_id == cat.id

    @pytest.mark.asyncio
    async def test_backfills_a_transfer_category(self, db, user, unlinked_account):
        transfer = await _category(db, user, "Card payment", CategoryType.TRANSFER)
        tx = await _uncategorised_tx(
            db, user, unlinked_account, "Online Payment - Thank You",
        )

        applied, locked = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, transfer.id, "online payment",
        )

        assert (applied, locked) == (1, 0)
        assert tx.category_id == transfer.id

    @pytest.mark.asyncio
    async def test_backfills_a_non_cash_category(self, db, user, unlinked_account):
        non_cash = await _category(db, user, "Interest", CategoryType.NON_CASH)
        tx = await _uncategorised_tx(db, user, unlinked_account, "Loan interest charged")

        applied, _ = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, non_cash.id, "interest charged",
        )

        assert applied == 1
        assert tx.category_id == non_cash.id

    @pytest.mark.asyncio
    async def test_does_not_cross_tenants(self, db, user, unlinked_account):
        cat = await _category(db, user)
        other_user_cat = Category(
            id=uuid.uuid4(), user_id=uuid.uuid4(), name="Theirs",
            category_type=CategoryType.EXPENSE, sort_order=0,
        )
        db.add(other_user_cat)
        await db.flush()
        tx = await _uncategorised_tx(db, user, unlinked_account, "COUNTDOWN ALBANY")

        applied, _ = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, other_user_cat.id, "countdown",
        )

        assert applied == 0
        assert tx.category_id is None
        assert cat.id != other_user_cat.id


class TestAddRuleEndpoint:
    @pytest.mark.asyncio
    async def test_accepts_a_transfer_category(self, db, user, unlinked_account):
        transfer = await _category(db, user, "Card payment", CategoryType.TRANSFER)
        tx = await _uncategorised_tx(
            db, user, unlinked_account, "Online Payment - Thank You",
        )

        resp = await mr_router.add_rule(
            category_id=transfer.id, keyword="Online Payment", user=user, db=db,
        )

        assert "applied=1" in resp.headers["location"]
        assert tx.category_id == transfer.id
        saved = (await db.execute(select(CategoryKeyword))).scalars().all()
        assert [kw.keyword for kw in saved] == ["online payment"]

    @pytest.mark.asyncio
    async def test_saves_and_backfills(self, db, user, unlinked_account):
        cat = await _category(db, user)
        tx = await _uncategorised_tx(db, user, unlinked_account, "COUNTDOWN ALBANY")

        resp = await mr_router.add_rule(
            category_id=cat.id, keyword="Countdown", user=user, db=db,
        )

        assert "applied=1" in resp.headers["location"]
        assert tx.category_id == cat.id


class TestPreviewCount:
    @pytest.mark.asyncio
    async def test_long_phrase_counts_substring_matches(self, db, user, unlinked_account):
        await _uncategorised_tx(db, user, unlinked_account, "COUNTDOWN ALBANY")
        await _uncategorised_tx(db, user, unlinked_account, "Hollywood Bakery")

        assert await mr_svc.count_uncategorized_matching(db, user.id, "countdown") == (1, 0)

    @pytest.mark.asyncio
    async def test_short_phrase_matches_the_engine(self, db, user, unlinked_account):
        await _uncategorised_tx(db, user, unlinked_account, "Loan repayment")
        await _uncategorised_tx(db, user, unlinked_account, "Processed on: 4 July")

        # Substring counting would say 2; the engine only matches the standalone "on".
        assert await mr_svc.count_uncategorized_matching(db, user.id, "on") == (1, 0)

    @pytest.mark.asyncio
    async def test_blank_phrase_is_zero(self, db, user):
        assert await mr_svc.count_uncategorized_matching(db, user.id, "  ") == (0, 0)

    @pytest.mark.asyncio
    async def test_reports_locked_rows_the_rule_cannot_move(self, db, user, unlinked_account):
        """The preview promised rows the backfill then refused to touch."""
        await _uncategorised_tx(db, user, unlinked_account, "Kmart Albany Auckland Nz")
        await _uncategorised_tx(
            db, user, unlinked_account, "Kmart Albany Auckland Nz 2", is_cleared=True,
        )

        total, locked = await mr_svc.count_uncategorized_matching(
            db, user.id, "kmart albany",
        )

        assert (total, locked) == (2, 1)

    @pytest.mark.asyncio
    async def test_preview_agrees_with_what_saving_does(self, db, user, unlinked_account):
        cat = await _category(db, user)
        await _uncategorised_tx(db, user, unlinked_account, "Kmart Albany Auckland Nz")
        await _uncategorised_tx(
            db, user, unlinked_account, "Kmart Albany Auckland Nz 2", is_cleared=True,
        )

        total, locked = await mr_svc.count_uncategorized_matching(
            db, user.id, "kmart albany",
        )
        applied, skipped = await mr_svc.apply_rule_to_uncategorised(
            db, user.id, cat.id, "kmart albany",
        )

        assert (applied, skipped) == (total - locked, locked)


class TestHitsCountAutomaticMatches:
    """"Hits" read 0 for rules that were quietly categorising every import."""

    @pytest.mark.asyncio
    async def test_import_records_a_hit(self, db, user, unlinked_account):
        cat = await _category(db, user)
        kw = await _keyword(db, cat, "countdown")
        stmt, lines = await _statement(
            db, user, unlinked_account, ["COUNTDOWN ALBANY", "COUNTDOWN GLENFIELD"],
        )

        await import_svc.import_statement_lines(
            db, user.id, stmt.id, [ln.id for ln in lines], unlinked_account.id,
        )

        assert kw.hit_count == 2

    @pytest.mark.asyncio
    async def test_only_the_winning_keyword_scores(self, db, user, unlinked_account):
        cat = await _category(db, user)
        broad = await _keyword(db, cat, "albany")
        specific = await _keyword(db, cat, "countdown albany")
        stmt, lines = await _statement(db, user, unlinked_account, ["COUNTDOWN ALBANY"])

        await import_svc.import_statement_lines(
            db, user.id, stmt.id, [ln.id for ln in lines], unlinked_account.id,
        )

        assert (specific.hit_count, broad.hit_count) == (1, 0)

    @pytest.mark.asyncio
    async def test_backfill_records_its_hits(self, db, user, unlinked_account):
        cat = await _category(db, user)
        await _uncategorised_tx(db, user, unlinked_account, "Kmart Albany Auckland Nz")
        await _uncategorised_tx(db, user, unlinked_account, "Kmart Albany Glenfield")

        await mr_router.add_rule(
            category_id=cat.id, keyword="kmart albany", user=user, db=db,
        )

        kw = (await db.execute(select(CategoryKeyword))).scalars().one()
        assert kw.hit_count == 2

    @pytest.mark.asyncio
    async def test_locked_rows_do_not_score(self, db, user, unlinked_account):
        cat = await _category(db, user)
        await _uncategorised_tx(
            db, user, unlinked_account, "Kmart Albany Auckland Nz", is_cleared=True,
        )

        await mr_router.add_rule(
            category_id=cat.id, keyword="kmart albany", user=user, db=db,
        )

        kw = (await db.execute(select(CategoryKeyword))).scalars().one()
        assert kw.hit_count == 0

    @pytest.mark.asyncio
    async def test_review_screen_still_only_suggests(self, db, user, unlinked_account):
        """Suggestions aren't matches — nothing is counted until it's applied."""
        cat = await _category(db, user)
        kw = await _keyword(db, cat, "countdown")
        await _uncategorised_tx(db, user, unlinked_account, "COUNTDOWN ALBANY")

        matches, _ = await categoriser.batch_suggest_categories(db, user.id)

        assert len(matches) == 1
        assert kw.hit_count == 0
