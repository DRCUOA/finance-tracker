"""Add cleared_amount to commitments for partial clearing

Revision ID: 011
Revises: 010
Create Date: 2026-04-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "commitments",
        sa.Column("cleared_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    # Back-fill: fully-cleared commitments get cleared_amount = amount
    op.execute(
        "UPDATE commitments SET cleared_amount = amount WHERE cleared_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("commitments", "cleared_amount")
