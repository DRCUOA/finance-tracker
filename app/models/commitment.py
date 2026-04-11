import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CommitmentDirection(str, enum.Enum):
    OUTFLOW = "outflow"
    INFLOW = "inflow"


class CommitmentConfidence(str, enum.Enum):
    CONFIRMED = "confirmed"
    EXPECTED = "expected"
    ESTIMATED = "estimated"


class CommitmentRecurrence(str, enum.Enum):
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUALLY = "annually"


class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    direction: Mapped[CommitmentDirection] = mapped_column(
        Enum(CommitmentDirection, values_callable=lambda x: [e.value for e in x]),
        nullable=False, default=CommitmentDirection.OUTFLOW,
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_recurring: Mapped[bool] = mapped_column(default=False, server_default="false")
    recurrence: Mapped[CommitmentRecurrence | None] = mapped_column(
        Enum(CommitmentRecurrence, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    confidence: Mapped[CommitmentConfidence] = mapped_column(
        Enum(CommitmentConfidence, values_callable=lambda x: [e.value for e in x]),
        nullable=False, default=CommitmentConfidence.CONFIRMED,
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="commitments")
    category = relationship("Category", back_populates="commitments")
