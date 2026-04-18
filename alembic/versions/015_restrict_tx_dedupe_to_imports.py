"""Exclude manual transactions from the per-account reference dedupe index

The original ``ix_transactions_account_reference_dedupe`` index made
``(account_id, reference)`` unique for every row whose ``reference`` was
not null. That broke manual entry: users naturally re-use generic
reference values like ``"Manual"`` across multiple manual transactions in
the same account, which is legitimate but tripped the unique constraint
(and during edits, prevented moving a manual transaction to an account
that already had another manual entry with the same reference).

The intent of the index is to deduplicate *imported* rows (OFX/CSV/Akahu),
where the bank-supplied reference is genuinely unique per account.

This migration narrows the partial-index predicate to exclude rows
explicitly tagged as manual (``source = 'manual'``) while preserving
existing dedupe behaviour for every other row, including the current
NULL-source population (which contains pre-tag manual rows alongside
legitimate OFX/CSV imports we do not yet differentiate; see CHANGELOG
"Known limitations").

Revision ID: 015
Revises: 014
Create Date: 2026-04-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "ix_transactions_account_reference_dedupe", table_name="transactions"
    )
    op.create_index(
        "ix_transactions_account_reference_dedupe",
        "transactions",
        ["account_id", "reference"],
        unique=True,
        postgresql_where="reference IS NOT NULL AND source IS DISTINCT FROM 'manual'",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_account_reference_dedupe", table_name="transactions"
    )
    op.create_index(
        "ix_transactions_account_reference_dedupe",
        "transactions",
        ["account_id", "reference"],
        unique=True,
        postgresql_where="reference IS NOT NULL",
    )
