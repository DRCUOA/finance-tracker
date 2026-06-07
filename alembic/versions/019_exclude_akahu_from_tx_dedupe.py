"""Exclude bank-feed (Akahu) rows from the per-account reference dedupe index

The ``ix_transactions_account_reference_dedupe`` index makes
``(account_id, reference)`` unique for imported rows. Migration 015 already
excluded ``source = 'manual'`` from it; this migration additionally excludes
``source = 'akahu'``.

Rationale: Akahu transactions are deduplicated by their stable, globally
unique ``akahu_transaction_id`` (the posted/pending sync upserts on
``(source, akahu_transaction_id)``), so the reference index gives them no
dedupe benefit. Worse, real bank feeds routinely reuse the same ``reference``
across genuinely distinct transactions in one account (e.g. recurring payees,
loan/transaction accounts where the bank populates a generic particulars
value). When two fetched rows shared a reference, the unique index raised an
IntegrityError that rolled back the *entire* sync batch — so a whole day's
worth of statement transactions silently failed to import.

The NULL-source population (legacy OFX/CSV imports we don't yet differentiate)
keeps its existing dedupe behaviour because ``NULL IS DISTINCT FROM 'akahu'``
is TRUE.

Revision ID: 019
Revises: 018
Create Date: 2026-06-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
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
        postgresql_where=(
            "reference IS NOT NULL "
            "AND source IS DISTINCT FROM 'manual' "
            "AND source IS DISTINCT FROM 'akahu'"
        ),
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
        postgresql_where="reference IS NOT NULL AND source IS DISTINCT FROM 'manual'",
    )
