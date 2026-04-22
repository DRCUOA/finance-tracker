import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AccountType(str, enum.Enum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    LOAN = "loan"
    INVESTMENT = "investment"
    CASH = "cash"
    OTHER = "other"


class AccountTerm(str, enum.Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class AccountGroup(str, enum.Enum):
    ASSET = "asset"
    LIABILITY = "liability"


CUMULATIVE_TERMS: dict[AccountTerm, list[AccountTerm]] = {
    AccountTerm.SHORT: [AccountTerm.SHORT],
    AccountTerm.MEDIUM: [AccountTerm.SHORT, AccountTerm.MEDIUM],
    AccountTerm.LONG: [AccountTerm.SHORT, AccountTerm.MEDIUM, AccountTerm.LONG],
}

ACCOUNT_TYPE_GROUPS = {
    AccountType.CHECKING: AccountGroup.ASSET,
    AccountType.SAVINGS: AccountGroup.ASSET,
    AccountType.INVESTMENT: AccountGroup.ASSET,
    AccountType.CASH: AccountGroup.ASSET,
    AccountType.CREDIT_CARD: AccountGroup.LIABILITY,
    AccountType.LOAN: AccountGroup.LIABILITY,
    AccountType.OTHER: AccountGroup.ASSET,
}


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(Enum(AccountType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    current_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    institution: Mapped[str | None] = mapped_column(String(100))
    term: Mapped[AccountTerm] = mapped_column(
        Enum(AccountTerm, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        server_default="short",
    )
    is_cashflow: Mapped[bool] = mapped_column(default=True, server_default="true")
    is_active: Mapped[bool] = mapped_column(default=True)
    akahu_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bank-reported balance (authoritative "where am I right now"), written by the
    # feed balance sync. NULL for unlinked accounts.
    reported_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # When the bank (or aggregator) says the reported balance applies to — distinct
    # from ``last_synced_at`` which is *our* clock at the moment we pulled it.
    reported_balance_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last time we successfully ingested posted transactions from the feed.
    transactions_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="accounts")
    transactions = relationship("Transaction", back_populates="account", cascade="all, delete-orphan")
    statements = relationship("Statement", back_populates="account", cascade="all, delete-orphan")
    reconciliations = relationship("Reconciliation", back_populates="account", cascade="all, delete-orphan")

    @property
    def group(self) -> AccountGroup:
        return ACCOUNT_TYPE_GROUPS.get(self.account_type, AccountGroup.ASSET)
