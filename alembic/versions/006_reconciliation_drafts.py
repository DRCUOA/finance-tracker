"""Add draft support to reconciliations

Adds status, draft_cleared_ids, and completed_at columns so users
can save an in-progress reconciliation and resume later.

Revision ID: 006
Revises: 005
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE reconciliationstatus AS ENUM('in_progress', 'completed')")

    op.add_column(
        "reconciliations",
        sa.Column(
            "status",
            sa.Enum("in_progress", "completed", name="reconciliationstatus", create_type=False),
            nullable=False,
            server_default="completed",
        ),
    )
    op.add_column(
        "reconciliations",
        sa.Column("draft_cleared_ids", sa.Text(), nullable=True),
    )
    op.add_column(
        "reconciliations",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE reconciliations SET completed_at = created_at")


def downgrade() -> None:
    op.drop_column("reconciliations", "completed_at")
    op.drop_column("reconciliations", "draft_cleared_ids")
    op.drop_column("reconciliations", "status")
    op.execute("DROP TYPE IF EXISTS reconciliationstatus")
