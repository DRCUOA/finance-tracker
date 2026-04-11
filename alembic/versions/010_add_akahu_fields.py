"""Add Akahu bank-feed fields to accounts and transactions

Revision ID: 010
Revises: 009
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- accounts: Akahu link ---
    op.add_column("accounts", sa.Column("akahu_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_accounts_akahu_id", "accounts", ["akahu_id"])

    # --- transactions: source-tracking & Akahu identity ---
    op.add_column("transactions", sa.Column("source", sa.String(20), nullable=True))
    op.add_column("transactions", sa.Column("akahu_transaction_id", sa.String(64), nullable=True))
    op.add_column("transactions", sa.Column("akahu_account_id", sa.String(64), nullable=True))
    op.add_column("transactions", sa.Column("akahu_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("transactions", sa.Column("is_pending", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("transactions", sa.Column("is_source_stale", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("transactions", sa.Column("source_stale_since", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_transactions_source", "transactions", ["source"])
    op.create_index(
        "uq_transactions_source_akahu_tx_id",
        "transactions",
        ["source", "akahu_transaction_id"],
        unique=True,
        postgresql_where=text("source IS NOT NULL AND akahu_transaction_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_transactions_source_akahu_tx_id", table_name="transactions")
    op.drop_index("ix_transactions_source", table_name="transactions")
    op.drop_column("transactions", "source_stale_since")
    op.drop_column("transactions", "is_source_stale")
    op.drop_column("transactions", "is_pending")
    op.drop_column("transactions", "akahu_updated_at")
    op.drop_column("transactions", "akahu_account_id")
    op.drop_column("transactions", "akahu_transaction_id")
    op.drop_column("transactions", "source")
    op.drop_constraint("uq_accounts_akahu_id", "accounts", type_="unique")
    op.drop_column("accounts", "akahu_id")
