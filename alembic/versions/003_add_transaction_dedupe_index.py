"""Add unique index for transaction deduplication

Revision ID: 003
Revises: 002
Create Date: 2026-04-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_account_reference_dedupe",
        "transactions",
        ["account_id", "reference"],
        unique=True,
        postgresql_where="reference IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_account_reference_dedupe", table_name="transactions")
