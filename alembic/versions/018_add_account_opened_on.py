"""Add user-editable opened_on date to accounts

Adds an ``opened_on`` Date column representing the real-world date the user
opened the underlying account (as opposed to ``created_at``, which is the
moment the row was inserted into our DB). Backdating ``opened_on`` lets the
retro interest re-evaluation walk further back than the system-tracked
creation timestamp — useful when a long-standing account is added to the
tracker mid-life.

Schema change:

    opened_on   Date NOT NULL
        Default for new rows: today (server-side ``CURRENT_DATE``).
        Backfilled for existing rows from ``created_at::date`` so the upgrade
        is non-destructive — every account keeps the open date it had
        implicitly before this column existed.

Revision ID: 018
Revises: 017
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Two-step add-then-tighten so existing rows can be backfilled before
    # NOT NULL is enforced.
    op.add_column(
        "accounts",
        sa.Column(
            "opened_on",
            sa.Date(),
            nullable=True,
            server_default=sa.text("CURRENT_DATE"),
        ),
    )
    # Backfill from the existing audit timestamp. CAST is portable enough for
    # both SQLite (used in tests) and PostgreSQL (prod).
    op.execute(
        "UPDATE accounts SET opened_on = CAST(created_at AS DATE) "
        "WHERE opened_on IS NULL"
    )
    op.alter_column("accounts", "opened_on", nullable=False)


def downgrade() -> None:
    op.drop_column("accounts", "opened_on")
