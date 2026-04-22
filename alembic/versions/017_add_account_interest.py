"""Add interest-accrual settings to accounts

Adds per-account interest configuration so the scheduler can accrue interest
against savings, loans, and credit products:

    interest_rate              Numeric(7, 4) nullable
        Annual percentage rate entered by the user (e.g. 4.5000 = 4.5% APR).
        NULL means interest accrual is disabled for this account.
    compounding_type           Enum(simple, compound) NOT NULL default 'compound'
        Whether interest is simple (principal-only) or compound (on balance
        incl. previously accrued interest).
    compounding_frequency      Enum(daily, monthly, quarterly, annually)
        NOT NULL default 'monthly'
        How often interest compounds per year. Used to convert APR into the
        per-period rate via the standard A = P(1 + r/n)^(n*t) formula.
    interest_last_accrued_at   DateTime(tz) nullable
        Most recent moment the scheduler posted an accrual transaction for
        this account. NULL until the first accrual. The scheduler uses the
        delta between now and this timestamp to compute how much to post.

Revision ID: 017
Revises: 016
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


compounding_type = sa.Enum("simple", "compound", name="compoundingtype")
compounding_frequency = sa.Enum(
    "daily", "monthly", "quarterly", "annually", name="compoundingfrequency"
)


def upgrade() -> None:
    compounding_type.create(op.get_bind(), checkfirst=True)
    compounding_frequency.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "accounts",
        sa.Column("interest_rate", sa.Numeric(7, 4), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "compounding_type",
            compounding_type,
            nullable=False,
            server_default="compound",
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "compounding_frequency",
            compounding_frequency,
            nullable=False,
            server_default="monthly",
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "interest_last_accrued_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "interest_last_accrued_at")
    op.drop_column("accounts", "compounding_frequency")
    op.drop_column("accounts", "compounding_type")
    op.drop_column("accounts", "interest_rate")
    compounding_frequency.drop(op.get_bind(), checkfirst=True)
    compounding_type.drop(op.get_bind(), checkfirst=True)
