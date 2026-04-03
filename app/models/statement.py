import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FileType(str, enum.Enum):
    CSV = "csv"
    OFX = "ofx"


class StatementStatus(str, enum.Enum):
    PENDING = "pending"
    IMPORTED = "imported"
    RECONCILED = "reconciled"


class MatchType(str, enum.Enum):
    EXACT = "exact"
    KEYWORD = "keyword"
    FUZZY = "fuzzy"
    MANUAL = "manual"
    NONE = "none"


class Statement(Base):
    __tablename__ = "statements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[FileType] = mapped_column(Enum(FileType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[StatementStatus] = mapped_column(Enum(StatementStatus, values_callable=lambda x: [e.value for e in x]), default=StatementStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account", back_populates="statements")
    lines = relationship("StatementLine", back_populates="statement", cascade="all, delete-orphan")


class StatementLine(Base):
    __tablename__ = "statement_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    statement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("statements.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100))
    matched_transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    match_type: Mapped[MatchType] = mapped_column(Enum(MatchType, values_callable=lambda x: [e.value for e in x]), default=MatchType.NONE)
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    statement = relationship("Statement", back_populates="lines")
    matched_transaction = relationship("Transaction", back_populates="statement_line", foreign_keys="Transaction.statement_line_id")
