"""Single balance authority for Finla.

The transaction ledger is the one source of truth for every monetary figure.
Balances are **derived on read** here and nowhere else — there is no cached
balance column to drift. The invariant this module guarantees, by
construction:

    balance(account, basis=ALL) == initial_balance + Σ(amount for that account)

``basis`` selects which slice of the ledger counts:

    ALL      every transaction (pending + posted)
    POSTED   settled transactions only (is_pending = False) — the DEFAULT for
             the headline account balance and net worth
    CLEARED  transactions confirmed on a finalised statement reconciliation
             (is_cleared = True); cleared ⊆ posted

``initial_balance`` (the opening seed) is always added regardless of basis —
the basis only filters the transaction sum.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountGroup
from app.models.transaction import Transaction

_ZERO = Decimal("0.00")


class BalanceBasis(str, enum.Enum):
    ALL = "all"
    POSTED = "posted"
    CLEARED = "cleared"


def _apply_basis(stmt, basis: BalanceBasis):
    """Translate ``basis`` into a WHERE clause — the only place this happens."""
    if basis is BalanceBasis.POSTED:
        return stmt.where(Transaction.is_pending.is_(False))
    if basis is BalanceBasis.CLEARED:
        return stmt.where(Transaction.is_cleared.is_(True))
    return stmt  # ALL — no filter


@dataclass(frozen=True)
class NetWorth:
    assets: Decimal
    liabilities: Decimal
    net: Decimal


async def balance(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    basis: BalanceBasis = BalanceBasis.POSTED,
    as_of: date | None = None,
) -> Decimal:
    """``initial_balance + Σ(amount)`` for one account, filtered by ``basis``.

    ``as_of`` is an inclusive upper bound on the transaction date.
    Returns ``0.00`` for an unknown account id.
    """
    acct = await session.get(Account, account_id)
    if acct is None:
        return _ZERO
    stmt = select(func.coalesce(func.sum(Transaction.amount), _ZERO)).where(
        Transaction.account_id == account_id
    )
    stmt = _apply_basis(stmt, basis)
    if as_of is not None:
        stmt = stmt.where(Transaction.date <= as_of)
    tx_sum = (await session.execute(stmt)).scalar() or _ZERO
    return acct.initial_balance + tx_sum


async def balances_for(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    basis: BalanceBasis = BalanceBasis.POSTED,
    as_of: date | None = None,
    account_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, Decimal]:
    """Bulk balances for a user's accounts in one grouped query (avoids N+1).

    Returns ``{account_id: balance}`` for every account owned by ``user_id``
    (optionally restricted to ``account_ids``), including accounts with no
    transactions (which return their ``initial_balance``). Always scoped to
    ``user_id`` for tenant isolation.
    """
    acct_stmt = select(Account.id, Account.initial_balance).where(
        Account.user_id == user_id
    )
    if account_ids is not None:
        ids = list(account_ids)
        if not ids:
            return {}
        acct_stmt = acct_stmt.where(Account.id.in_(ids))
    balances: dict[uuid.UUID, Decimal] = {
        aid: init for aid, init in (await session.execute(acct_stmt)).all()
    }
    if not balances:
        return {}

    sum_stmt = select(
        Transaction.account_id,
        func.coalesce(func.sum(Transaction.amount), _ZERO),
    ).where(Transaction.account_id.in_(balances.keys()))
    sum_stmt = _apply_basis(sum_stmt, basis)
    if as_of is not None:
        sum_stmt = sum_stmt.where(Transaction.date <= as_of)
    sum_stmt = sum_stmt.group_by(Transaction.account_id)

    for aid, tx_sum in (await session.execute(sum_stmt)).all():
        balances[aid] = balances[aid] + (tx_sum or _ZERO)
    return balances


def aggregate_net_worth(
    accounts: Sequence[Account],
    balances: dict[uuid.UUID, Decimal],
) -> NetWorth:
    """Roll a set of accounts + their balances into assets/liabilities/net.

    The single asset/liability split used by every net-worth surface. Assets
    contribute their signed balance; liabilities contribute ``abs(balance)``.
    """
    assets = _ZERO
    liabilities = _ZERO
    for acct in accounts:
        bal = balances.get(acct.id, acct.initial_balance)
        if acct.group == AccountGroup.ASSET:
            assets += bal
        else:
            liabilities += abs(bal)
    return NetWorth(assets=assets, liabilities=liabilities, net=assets - liabilities)


async def net_worth(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> NetWorth:
    """Canonical net worth over a user's **active** accounts (POSTED basis)."""
    accounts = list(
        (
            await session.execute(
                select(Account).where(
                    Account.user_id == user_id,
                    Account.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    balances = await balances_for(
        session,
        user_id,
        basis=BalanceBasis.POSTED,
        as_of=as_of,
        account_ids=[a.id for a in accounts],
    )
    return aggregate_net_worth(accounts, balances)
