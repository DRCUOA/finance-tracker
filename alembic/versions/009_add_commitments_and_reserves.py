"""Add commitments table and category reserve_amount for live position tracking

Revision ID: 009
Revises: 008
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "commitments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category_id", UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("direction", sa.Enum("outflow", "inflow", name="commitmentdirection"), nullable=False, server_default="outflow"),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("recurrence", sa.Enum("weekly", "fortnightly", "monthly", "quarterly", "annually", name="commitmentrecurrence"), nullable=True),
        sa.Column("confidence", sa.Enum("confirmed", "expected", "estimated", name="commitmentconfidence"), nullable=False, server_default="confirmed"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_commitments_user_due", "commitments", ["user_id", "due_date"])

    op.add_column(
        "categories",
        sa.Column("reserve_amount", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0.00")),
    )


def downgrade() -> None:
    op.drop_column("categories", "reserve_amount")
    op.drop_index("ix_commitments_user_due", table_name="commitments")
    op.drop_table("commitments")
    op.execute("DROP TYPE IF EXISTS commitmentdirection")
    op.execute("DROP TYPE IF EXISTS commitmentrecurrence")
    op.execute("DROP TYPE IF EXISTS commitmentconfidence")
