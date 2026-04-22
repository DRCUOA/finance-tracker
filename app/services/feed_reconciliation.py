"""Feed-reconciliation snapshot.

The bank feed (Akahu etc.) gives us two pieces of ground truth: a *reported
balance* and a stream of posted transactions. These two lag each other — the
bank publishes a current balance within minutes of a swipe, but the posted
transaction can take days to arrive. If the app naively derives balance from
transactions, reports drift behind the bank.

Rather than fabricate a plug transaction to close the gap, this module
computes an explicit reconciliation view per account:

    reported_balance        what the bank says the account is right now
    posted_balance          initial_balance + sum(posted transactions)
    pending_total           sum of amounts in the pending-transaction layer
    unreconciled_delta      reported_balance - posted_balance - pending_total
                            i.e. what the bank shows that we can't yet
                            explain from posted + pending transactions

All aggregation queries (budgets, category spend, cashflow, etc.) continue to
use *posted* transactions only. Pending is shown in its own UI row; the
unreconciled delta is shown as a labelled reconciliation row — never as a
synthetic transaction in any category.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction


# How long the feed can lag before we flip the account from "in_sync" to
# "lagging", and from "lagging" to "stale". These are deliberately generous —
# the UX goal is to tell the user *why* their balance doesn't match their
# reports, not to flag every sub-day drift.
LAG_THRESHOLD_HOURS = 12
STALE_THRESHOLD_HOURS = 72

# Any absolute delta below this is treated as noise — rounding from the feed,
# a fee Akahu exposes in its balance but not yet as a transaction, etc.
DELTA_NOISE_FLOOR = Decimal("0.01")


@dataclass(frozen=True)
class AccountFeedStatus:
    account_id: uuid.UUID
    account_name: str
    currency: str
    is_linked: bool
    # Balance views — all optional because an unlinked account has no feed.
    reported_balance: Decimal | None
    posted_balance: Decimal
    pending_total: Decimal
    unreconciled_delta: Decimal | None
    # Freshness — ``reported_balance_as_of`` is the bank's own clock (what
    # the user cares about); ``last_synced_at`` is our own.
    reported_balance_as_of: datetime | None
    transactions_as_of: datetime | None
    last_synced_at: datetime | None
    feed_lag_hours: float | None
    # Classification derived from the above: one of
    # "unlinked" | "in_sync" | "lagging" | "stale" | "no_delta_info"
    health: str

    def as_dict(self) -> dict:
        """Serialise for template/API rendering."""
        return {
            "account_id": str(self.account_id),
            "account_name": self.account_name,
            "currency": self.currency,
            "is_linked": self.is_linked,
            "reported_balance": (
                float(self.reported_balance) if self.reported_balance is not None else None
            ),
            "posted_balance": float(self.posted_balance),
            "pending_total": float(self.pending_total),
            "unreconciled_delta": (
                float(self.unreconciled_delta)
                if self.unreconciled_delta is not None
                else None
            ),
            "reported_balance_as_of": (
                self.reported_balance_as_of.isoformat()
                if self.reported_balance_as_of
                else None
            ),
            "transactions_as_of": (
                self.transactions_as_of.isoformat() if self.transactions_as_of else None
            ),
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "feed_lag_hours": self.feed_lag_hours,
            "health": self.health,
        }


def _classify(
    is_linked: bool,
    unreconciled_delta: Decimal | None,
    reported_balance_as_of: datetime | None,
    now: datetime,
) -> tuple[str, float | None]:
    """Return (health, feed_lag_hours)."""
    if not is_linked:
        return "unlinked", None

    lag_hours: float | None = None
    if reported_balance_as_of is not None:
        # Normalise timezone-naive timestamps to UTC to stay robust against
        # drivers that strip tzinfo on read.
        ref = reported_balance_as_of
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        lag_hours = max((now - ref).total_seconds() / 3600.0, 0.0)

    # No delta info yet — balance has never been reported by the feed.
    if unreconciled_delta is None:
        return "no_delta_info", lag_hours

    in_sync_by_delta = abs(unreconciled_delta) < DELTA_NOISE_FLOOR
    if lag_hours is None:
        return ("in_sync" if in_sync_by_delta else "lagging"), None

    if lag_hours >= STALE_THRESHOLD_HOURS:
        return "stale", lag_hours
    if not in_sync_by_delta or lag_hours >= LAG_THRESHOLD_HOURS:
        return "lagging", lag_hours
    return "in_sync", lag_hours


async def account_feed_status(
    db: AsyncSession,
    account: Account,
    *,
    now: datetime | None = None,
) -> AccountFeedStatus:
    """Compute the feed-reconciliation snapshot for a single account."""
    now = now or datetime.now(timezone.utc)

    posted_sum_stmt = select(
        sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00"))
    ).where(
        Transaction.account_id == account.id,
        Transaction.is_pending.is_(False),
    )
    pending_sum_stmt = select(
        sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00"))
    ).where(
        Transaction.account_id == account.id,
        Transaction.is_pending.is_(True),
    )
    posted_tx_sum = (await db.execute(posted_sum_stmt)).scalar() or Decimal("0.00")
    pending_total = (await db.execute(pending_sum_stmt)).scalar() or Decimal("0.00")

    posted_balance = account.initial_balance + posted_tx_sum

    is_linked = bool(account.akahu_id)
    reported = account.reported_balance if is_linked else None
    delta: Decimal | None
    if reported is None:
        delta = None
    else:
        delta = (reported - posted_balance - pending_total).quantize(Decimal("0.01"))

    health, lag_hours = _classify(
        is_linked, delta, account.reported_balance_as_of, now
    )

    return AccountFeedStatus(
        account_id=account.id,
        account_name=account.name,
        currency=account.currency,
        is_linked=is_linked,
        reported_balance=reported,
        posted_balance=posted_balance,
        pending_total=pending_total,
        unreconciled_delta=delta,
        reported_balance_as_of=account.reported_balance_as_of,
        transactions_as_of=account.transactions_as_of,
        last_synced_at=account.last_synced_at,
        feed_lag_hours=lag_hours,
        health=health,
    )


async def user_feed_status(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    account_ids: Iterable[uuid.UUID] | None = None,
    linked_only: bool = False,
    now: datetime | None = None,
) -> list[AccountFeedStatus]:
    """Feed-reconciliation snapshots for every (active) account of a user.

    Pass ``linked_only=True`` to suppress manual/CSV accounts — useful on the
    reports page where an unlinked account would just render as "unlinked"
    noise.
    """
    stmt = select(Account).where(
        Account.user_id == user_id,
        Account.is_active.is_(True),
    )
    if account_ids is not None:
        stmt = stmt.where(Account.id.in_(list(account_ids)))
    if linked_only:
        stmt = stmt.where(Account.akahu_id.isnot(None))
    stmt = stmt.order_by(Account.sort_order, Account.name)

    accounts = list((await db.execute(stmt)).scalars().all())
    return [await account_feed_status(db, a, now=now) for a in accounts]
