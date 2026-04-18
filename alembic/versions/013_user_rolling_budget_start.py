"""Add rolling_budget_start to users for spending rolling report

Revision ID: 013
Revises: 012
Create Date: 2026-04-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("rolling_budget_start", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "rolling_budget_start")
