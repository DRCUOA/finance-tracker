"""Cross-source transaction dedup identity (compartment #2)

Adds the content-based dedup identity that holds for **every** source incl.
manual: ``content_hash`` (account-independent digest of date+amount+normalised
description), ``occurrence`` (discriminates deliberately-admitted repeats), and
``dedup_override`` (audit marker). Backed by
``unique(account_id, content_hash, occurrence)``.

Backfill (spec-02 §8.2 — auto-discriminate, keep all): every existing row is
hashed **in Python through app.services.dedup** so the values match runtime
byte-for-byte. Within each (account_id, content_hash) group, rows get ascending
occurrence numbers ordered by (created_at, id); any occurrence > 0 (a
pre-existing duplicate) is also flagged dedup_override. Nothing is deleted.

The old reference-uniqueness index (``ix_transactions_account_reference_dedupe``,
migrations 003/015/019) is dropped: ``reference`` is no longer the dedup signal,
and its uniqueness was the cause of the reused-reference silent-drop. Akahu's
``uq_transactions_source_akahu_tx_id`` is kept untouched.

Revision ID: 022
Revises: 021
Create Date: 2026-06-13
"""
from collections import defaultdict
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.services.dedup import content_hash

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column(
        "transactions",
        sa.Column("occurrence", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "transactions",
        sa.Column("dedup_override", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_transactions_account_content_hash",
        "transactions",
        ["account_id", "content_hash"],
    )

    # --- Python backfill (auto-discriminate, keep all) ---------------------
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, account_id, date, amount, description "
            "FROM transactions ORDER BY account_id, created_at, id"
        )
    ).fetchall()

    seen: dict[tuple, int] = defaultdict(int)
    for row in rows:
        ch = content_hash(row.date, row.amount, row.description)
        key = (row.account_id, ch)
        occ = seen[key]
        seen[key] += 1
        bind.execute(
            sa.text(
                "UPDATE transactions "
                "SET content_hash = :ch, occurrence = :occ, dedup_override = :ovr "
                "WHERE id = :id"
            ),
            {"ch": ch, "occ": occ, "ovr": occ > 0, "id": row.id},
        )

    op.alter_column("transactions", "content_hash", nullable=False)
    op.create_unique_constraint(
        "uq_transactions_account_content_occurrence",
        "transactions",
        ["account_id", "content_hash", "occurrence"],
    )

    # reference is no longer identity — drop its uniqueness (see module docstring).
    op.drop_index("ix_transactions_account_reference_dedupe", table_name="transactions")


def downgrade() -> None:
    # Re-create the reference dedupe index as left by migration 019.
    op.create_index(
        "ix_transactions_account_reference_dedupe",
        "transactions",
        ["account_id", "reference"],
        unique=True,
        postgresql_where=(
            "reference IS NOT NULL "
            "AND source IS DISTINCT FROM 'manual' "
            "AND source IS DISTINCT FROM 'akahu'"
        ),
    )
    op.drop_constraint(
        "uq_transactions_account_content_occurrence", "transactions", type_="unique"
    )
    op.drop_index("ix_transactions_account_content_hash", table_name="transactions")
    op.drop_column("transactions", "dedup_override")
    op.drop_column("transactions", "occurrence")
    op.drop_column("transactions", "content_hash")
