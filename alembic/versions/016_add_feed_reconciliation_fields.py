"""Add bank-reported balance & freshness fields for feed reconciliation

Separates the bank-side ("reported") balance from the transaction-derived
balance so reports can surface the gap between them rather than silently
flip-flopping ``current_balance`` depending on which sync ran last.

New columns on ``accounts``:
    reported_balance            Numeric(14, 2) nullable
        The balance most recently reported by the bank feed. ``NULL`` when
        the account has never been synced via a linked feed (manual,
        CSV, OFX, etc.).
    reported_balance_as_of      DateTime(tz) nullable
        Timestamp the feed says the reported balance applies to
        (e.g. Akahu's ``refreshed.balance``). Distinct from
        ``last_synced_at`` which is *our* sync moment — the bank may be
        hours behind its own refresh.
    transactions_as_of          DateTime(tz) nullable
        Most recent posted-transaction ingestion timestamp from the feed.

Revision ID: 016
Revises: 015
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("reported_balance", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("reported_balance_as_of", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("transactions_as_of", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "transactions_as_of")
    op.drop_column("accounts", "reported_balance_as_of")
    op.drop_column("accounts", "reported_balance")
