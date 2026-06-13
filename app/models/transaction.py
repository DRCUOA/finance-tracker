import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Compartment #2: the cross-source dedup backstop, covering every source
        # including manual. A deliberate repeat is admitted with a distinct
        # occurrence; an accidental re-import collides at occurrence 0.
        UniqueConstraint(
            "account_id", "content_hash", "occurrence",
            name="uq_transactions_account_content_occurrence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"))
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    original_description: Mapped[str | None] = mapped_column(String(500))
    reference: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    is_cleared: Mapped[bool] = mapped_column(default=False)

    source: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    akahu_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    akahu_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    akahu_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_pending: Mapped[bool] = mapped_column(default=False, server_default="false")
    is_source_stale: Mapped[bool] = mapped_column(default=False, server_default="false")
    source_stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Compartment #2 (ingestion integrity). content_hash is the cross-source
    # content identity; occurrence discriminates deliberately-admitted repeats;
    # dedup_override marks a repeat that was admitted via the override path.
    # Backstopped by unique(account_id, content_hash, occurrence) — see migration 022.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    occurrence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    dedup_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="transactions")
    account = relationship("Account", back_populates="transactions")
    category = relationship("Category", back_populates="transactions")
