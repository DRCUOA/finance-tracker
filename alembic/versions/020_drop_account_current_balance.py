"""Drop the denormalised accounts.current_balance cache

Compartment #1 (single balance authority): the transaction ledger is the one
source of truth for balances, which are now derived on read via
``app.services.balances.balance``. The cached ``current_balance`` column was a
drift source (the transaction service never updated it; interest incremented
it; the migration importer seeded it including pending rows) and is removed.

Reversible: the down-migration re-adds the column and backfills it from
``initial_balance + Σ(posted transactions)`` — the POSTED-basis definition the
old ``recalculate_balance`` used.

Revision ID: 020
Revises: 019
Create Date: 2026-06-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("accounts", "current_balance")


def downgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "current_balance",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        """
        UPDATE accounts
        SET current_balance = initial_balance + COALESCE(
            (
                SELECT SUM(t.amount)
                FROM transactions t
                WHERE t.account_id = accounts.id
                  AND t.is_pending = false
            ),
            0
        )
        """
    )
