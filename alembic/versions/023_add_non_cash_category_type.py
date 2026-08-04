"""Add non_cash category type; backfill existing non-cash sprawl into it

Pure asset/liability value movements (auto-accrued interest, revaluations,
pension-fund growth) are not cash and must stay out of spending / budget /
income-vs-expense views. Historically they were approximated three ways:
transfer-typed categories ("Non-Cash Adjustments" from migration 007), an
expense-typed "Mortgage Interest" category (the leak: it counted as cash
spending), and uncategorised ``source = 'interest'`` accrual transactions.
This migration centres all of it on the new ``non_cash`` enum value:

1. adds ``non_cash`` to the ``categorytype`` enum;
2. retypes every top-level "Non-Cash Adjustments" category and all of its
   descendants from ``transfer`` to ``non_cash`` (any user — the name is
   unambiguous);
3. for the single-user data that migration 007 curated, retypes "Mortgage
   Interest" to ``non_cash`` and reparents it under that user's non-cash
   root — its transactions are loan-ledger interest accruals, which is
   exactly the mis-statement being cleaned up;
4. for every user with ``source = 'interest'`` transactions, resolves (or
   creates) a top-level non-cash root and an "Interest" child under it,
   then recategorises all those transactions there — including rows a user
   had manually filed under an expense category.

Postgres cannot use a freshly added enum value inside the transaction that
added it, so the ALTER TYPE runs in an autocommit block first.

Downgrade retypes ``non_cash`` categories to ``transfer`` (still excluded
from cash views) rather than deleting them — user categories and their
transaction links are never destroyed. The enum value itself remains, as
Postgres cannot drop enum values without rebuilding the type.

Revision ID: 023
Revises: 022
Create Date: 2026-08-04
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The user whose data migration 007 curated; the Mortgage Interest retype is
# scoped to them because the category name is theirs, not a product concept.
UID_007 = "a6beaf68-88fd-4e8e-8dfc-6919a631456a"

NON_CASH_ROOT_NAME = "Non-Cash"
INTEREST_CHILD_NAME = "Interest"


def _resolve_non_cash_root(bind, user_id) -> str | None:
    """First top-level non_cash category for the user, if any.

    Falls back to a well-known root *name* regardless of type and retypes it
    — this makes upgrade→downgrade→upgrade converge on the same root instead
    of creating a duplicate (downgrade parks non_cash categories as
    transfer, which would otherwise hide them from the type-based lookup).
    """
    root_id = bind.execute(
        sa.text(
            "SELECT id FROM categories WHERE user_id = :uid "
            "AND category_type = 'non_cash' AND parent_id IS NULL "
            "ORDER BY sort_order LIMIT 1"
        ),
        {"uid": user_id},
    ).scalar()
    if root_id is not None:
        return root_id

    root_id = bind.execute(
        sa.text(
            "SELECT id FROM categories WHERE user_id = :uid "
            "AND name IN ('Non-Cash Adjustments', :root_name) "
            "AND parent_id IS NULL ORDER BY sort_order LIMIT 1"
        ),
        {"uid": user_id, "root_name": NON_CASH_ROOT_NAME},
    ).scalar()
    if root_id is not None:
        bind.execute(
            sa.text("UPDATE categories SET category_type = 'non_cash' WHERE id = :id"),
            {"id": root_id},
        )
    return root_id


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE categorytype ADD VALUE IF NOT EXISTS 'non_cash'")
    # Test databases (SQLite) are built from current model metadata via
    # create_all, so there is no enum DDL to alter on other dialects.

    # ── 2. Retype "Non-Cash Adjustments" trees ───────────────────────────
    bind.execute(
        sa.text(
            "WITH RECURSIVE nca AS ("
            "  SELECT id FROM categories "
            "  WHERE name = 'Non-Cash Adjustments' AND parent_id IS NULL "
            "  UNION ALL "
            "  SELECT c.id FROM categories c JOIN nca ON c.parent_id = nca.id"
            ") "
            "UPDATE categories SET category_type = 'non_cash' "
            "WHERE id IN (SELECT id FROM nca)"
        )
    )

    # ── 3. Mortgage Interest (007's user): expense → non_cash, reparented ─
    mi_id = bind.execute(
        sa.text(
            "SELECT id FROM categories WHERE user_id = :uid "
            "AND name = 'Mortgage Interest' LIMIT 1"
        ),
        {"uid": UID_007},
    ).scalar()
    if mi_id is not None:
        root_007 = _resolve_non_cash_root(bind, UID_007)
        bind.execute(
            sa.text(
                "UPDATE categories SET category_type = 'non_cash', "
                "parent_id = COALESCE(:root, parent_id) WHERE id = :id"
            ),
            {"root": root_007, "id": mi_id},
        )

    # ── 4. File interest-accrual transactions under a non-cash category ──
    users = bind.execute(
        sa.text("SELECT DISTINCT user_id FROM transactions WHERE source = 'interest'")
    ).fetchall()

    for (user_id,) in users:
        root_id = _resolve_non_cash_root(bind, user_id)
        if root_id is None:
            root_id = str(uuid.uuid4())
            next_order = bind.execute(
                sa.text(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories "
                    "WHERE user_id = :uid AND parent_id IS NULL"
                ),
                {"uid": user_id},
            ).scalar()
            bind.execute(
                sa.text(
                    "INSERT INTO categories "
                    "(id, user_id, name, category_type, sort_order, "
                    " budgeted_amount, reserve_amount, is_fixed) "
                    "VALUES (:id, :uid, :name, 'non_cash', :ord, 0.00, 0.00, false)"
                ),
                {"id": root_id, "uid": user_id, "name": NON_CASH_ROOT_NAME, "ord": next_order},
            )

        # Match by name only (not type) so a child parked as transfer by a
        # prior downgrade is reclaimed instead of duplicated.
        interest_id = bind.execute(
            sa.text(
                "SELECT id FROM categories WHERE user_id = :uid "
                "AND name = :name AND parent_id = :pid LIMIT 1"
            ),
            {"uid": user_id, "name": INTEREST_CHILD_NAME, "pid": root_id},
        ).scalar()
        if interest_id is not None:
            bind.execute(
                sa.text("UPDATE categories SET category_type = 'non_cash' WHERE id = :id"),
                {"id": interest_id},
            )
        else:
            next_child_order = bind.execute(
                sa.text(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories "
                    "WHERE user_id = :uid AND parent_id = :pid"
                ),
                {"uid": user_id, "pid": root_id},
            ).scalar()
            interest_id = str(uuid.uuid4())
            bind.execute(
                sa.text(
                    "INSERT INTO categories "
                    "(id, user_id, name, category_type, parent_id, sort_order, "
                    " budgeted_amount, reserve_amount, is_fixed) "
                    "VALUES (:id, :uid, :name, 'non_cash', :pid, :ord, 0.00, 0.00, false)"
                ),
                {"id": interest_id, "uid": user_id, "name": INTEREST_CHILD_NAME,
                 "pid": root_id, "ord": next_child_order},
            )

        bind.execute(
            sa.text(
                "UPDATE transactions SET category_id = :cid "
                "WHERE user_id = :uid AND source = 'interest'"
            ),
            {"cid": interest_id, "uid": user_id},
        )


def downgrade() -> None:
    # Non-destructive: park non_cash categories as transfer (still excluded
    # from cash views) so no category or transaction link is lost. The enum
    # value itself stays — Postgres cannot drop enum values in place.
    op.execute("UPDATE categories SET category_type = 'transfer' WHERE category_type = 'non_cash'")
