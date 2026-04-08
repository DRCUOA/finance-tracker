"""Replace statement-based reconciliation with ending-balance reconciliation

- Add is_cleared to transactions (migrated from is_reconciled)
- Drop is_reconciled and statement_line_id from transactions
- Drop match columns from statement_lines
- Clean up matchtype and statementstatus enums
- Create reconciliations table

Revision ID: 005
Revises: 004
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- transactions: add is_cleared, migrate data, drop old columns ---
    op.add_column(
        "transactions",
        sa.Column("is_cleared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE transactions SET is_cleared = is_reconciled")

    op.drop_constraint(
        "transactions_statement_line_id_fkey", "transactions", type_="foreignkey",
    )
    op.drop_column("transactions", "statement_line_id")
    op.drop_column("transactions", "is_reconciled")

    # --- statement_lines: drop reconciliation columns ---
    op.drop_column("statement_lines", "matched_transaction_id")
    op.drop_column("statement_lines", "match_type")
    op.drop_column("statement_lines", "match_confidence")

    # --- drop matchtype enum ---
    op.execute("DROP TYPE IF EXISTS matchtype")

    # --- remove 'reconciled' from statementstatus enum ---
    op.execute("ALTER TYPE statementstatus RENAME TO statementstatus_old")
    op.execute("CREATE TYPE statementstatus AS ENUM('pending', 'imported')")
    op.execute(
        "ALTER TABLE statements ALTER COLUMN status TYPE statementstatus "
        "USING status::text::statementstatus"
    )
    op.execute("DROP TYPE statementstatus_old")

    # --- create reconciliations table ---
    op.create_table(
        "reconciliations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("statement_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("cleared_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("reconciliations")

    op.execute("ALTER TYPE statementstatus RENAME TO statementstatus_old")
    op.execute("CREATE TYPE statementstatus AS ENUM('pending', 'imported', 'reconciled')")
    op.execute(
        "ALTER TABLE statements ALTER COLUMN status TYPE statementstatus "
        "USING status::text::statementstatus"
    )
    op.execute("DROP TYPE statementstatus_old")

    op.execute(
        "CREATE TYPE matchtype AS ENUM('exact', 'keyword', 'fuzzy', 'manual', 'none')"
    )
    op.add_column("statement_lines", sa.Column("match_confidence", sa.Float(), server_default="0.0"))
    op.add_column("statement_lines", sa.Column(
        "match_type",
        postgresql.ENUM("exact", "keyword", "fuzzy", "manual", "none", name="matchtype", create_type=False),
        server_default="none",
    ))
    op.add_column("statement_lines", sa.Column(
        "matched_transaction_id", postgresql.UUID(as_uuid=True), nullable=True,
    ))

    op.add_column("transactions", sa.Column("is_reconciled", sa.Boolean(), server_default=sa.text("false")))
    op.execute("UPDATE transactions SET is_reconciled = is_cleared")
    op.add_column("transactions", sa.Column(
        "statement_line_id", postgresql.UUID(as_uuid=True), nullable=True,
    ))
    op.create_foreign_key(
        "transactions_statement_line_id_fkey", "transactions", "statement_lines",
        ["statement_line_id"], ["id"], ondelete="SET NULL",
    )
    op.drop_column("transactions", "is_cleared")
