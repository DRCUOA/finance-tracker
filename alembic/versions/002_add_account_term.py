"""Add account term column

Revision ID: 002
Revises: 001
Create Date: 2026-04-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

account_term = sa.Enum("short", "medium", "long", name="accountterm")


def upgrade() -> None:
    account_term.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "accounts",
        sa.Column("term", account_term, nullable=False, server_default="short"),
    )


def downgrade() -> None:
    op.drop_column("accounts", "term")
    account_term.drop(op.get_bind(), checkfirst=True)
