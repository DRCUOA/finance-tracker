"""Interest accrual.

Two responsibilities, kept separate so the math is testable without a DB:

- :func:`compute_interest` — pure function. Given a principal, APR, compounding
  type/frequency, and elapsed time, return the interest amount (rounded to
  cents). No I/O. Exhaustively unit-tested.

- :func:`accrue_interest_for_account` / :func:`accrue_due_interest` — async
  orchestrators. Post an interest transaction for one account (or every
  eligible account) and stamp ``interest_last_accrued_at`` so the next run
  picks up from where this one left off.

Sign conventions
----------------
We apply interest in the direction of the account's group:

* **Asset** accounts (checking, savings, investment, cash): a positive
  transaction increases the balance. Interest on a *negative* asset balance
  (e.g. an overdrawn checking account) still posts a positive amount — the
  user is earning interest on a bank-owed liability, which the ledger already
  models via the sign of the balance.
* **Liability** accounts (credit_card, loan): interest *charges* make the
  debt grow, which in this ledger means the balance becomes more negative.
  So we post a negative transaction.

Principal is ``abs(current_balance)`` in both cases so the math is always done
on a positive number; the group supplies the sign.

Cadence
-------
The scheduler invokes :func:`accrue_due_interest` daily. Each call prorates
the time since ``interest_last_accrued_at`` (or the account's ``created_at``
for the first run) as a fraction of a year, then posts one transaction
covering that window. Running daily vs. weekly yields the same final balance
because we carry the cumulative timestamp forward — the choice of run cadence
only affects how often the interest transaction appears in the ledger.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import (
    Account,
    AccountGroup,
    CompoundingFrequency,
    CompoundingType,
    PERIODS_PER_YEAR,
)
from app.models.transaction import Transaction


INTEREST_SOURCE = "interest"
DAYS_PER_YEAR = Decimal("365")
CENT = Decimal("0.01")


def compute_interest(
    principal: Decimal,
    apr_percent: Decimal,
    compounding_type: CompoundingType,
    compounding_frequency: CompoundingFrequency,
    elapsed_days: int,
) -> Decimal:
    """Compute interest on ``principal`` over ``elapsed_days``.

    ``apr_percent`` is the annual rate expressed in percent (4.5 = 4.5% APR).
    ``elapsed_days`` must be a non-negative integer; fractional days are
    intentionally not supported because callers always work in whole-day
    increments (the scheduler timestamps accruals at midnight UTC).

    Returns the interest amount as a positive :class:`Decimal` rounded to
    cents (``0.01``). Callers are responsible for applying the sign based on
    the account group. Zero principal, zero rate, or zero elapsed days all
    return ``Decimal("0.00")``.

    Compound branch uses the standard ``A = P(1 + r/n)^(n*t)`` formula with
    ``t = elapsed_days / 365`` in years; ``Decimal`` lacks a non-integer
    ``**`` operator, so the exponentiation is done in float. The final
    quantize to cents absorbs the tiny float error.
    """
    if principal <= 0 or apr_percent <= 0 or elapsed_days <= 0:
        return Decimal("0.00")

    years = Decimal(elapsed_days) / DAYS_PER_YEAR
    rate = Decimal(apr_percent) / Decimal("100")

    if compounding_type == CompoundingType.SIMPLE:
        interest = principal * rate * years
    else:  # COMPOUND
        n = PERIODS_PER_YEAR[compounding_frequency]
        rate_per_period = float(rate) / n
        total_periods = n * float(years)
        multiplier = (1.0 + rate_per_period) ** total_periods
        interest = principal * Decimal(str(multiplier - 1.0))

    return interest.quantize(CENT, rounding=ROUND_HALF_UP)


def _account_is_accrual_eligible(acct: Account) -> bool:
    """Return True if ``acct`` has interest configured AND a non-zero balance.

    A rate of 0 or a rate that is NULL means the user hasn't opted this
    account in. A zero balance means there's nothing to accrue on (matters
    for credit cards that currently sit at $0 — no debt, no charge).
    """
    if acct.interest_rate is None or acct.interest_rate == 0:
        return False
    if acct.current_balance == 0:
        return False
    return True


async def accrue_interest_for_account(
    db: AsyncSession,
    account: Account,
    *,
    now: datetime | None = None,
) -> Transaction | None:
    """Post accrued interest for ``account`` and stamp it.

    Returns the created :class:`Transaction`, or ``None`` if nothing was
    accrued (rate unconfigured, zero balance, zero elapsed days, or the
    computed amount rounded to 0.00).

    The posted transaction has:
      * ``source = "interest"`` — lets reports filter interest out of
        discretionary spending if desired.
      * ``amount`` signed by account group (positive for assets, negative
        for liabilities). See module docstring.
      * ``date`` set to today (the day the accrual posts).
      * ``description`` "Interest" plus the accrual window for traceability.

    The caller owns committing the session; this function only ``flush``-es
    so the transaction is visible for balance recalculation.
    """
    if not _account_is_accrual_eligible(account):
        return None

    now = now or datetime.now(timezone.utc)
    start = account.interest_last_accrued_at or account.created_at
    if start is None:
        # Fallback for freshly created accounts with no ``created_at`` yet;
        # treat "now" as the baseline so the next run has a reference.
        account.interest_last_accrued_at = now
        return None

    # Normalise to tz-aware UTC; SQLite returns naive datetimes.
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    elapsed_days = (now - start).days
    if elapsed_days <= 0:
        return None

    principal = abs(account.current_balance)
    interest = compute_interest(
        principal,
        account.interest_rate,
        account.compounding_type,
        account.compounding_frequency,
        elapsed_days,
    )
    if interest == 0:
        # Still advance the timestamp — otherwise a tiny principal that
        # accrues < 0.005 per day would never catch up.
        account.interest_last_accrued_at = now
        return None

    sign = Decimal(1) if account.group == AccountGroup.ASSET else Decimal(-1)
    amount = sign * interest

    tx = Transaction(
        user_id=account.user_id,
        account_id=account.id,
        date=now.date(),
        amount=amount,
        description=f"Interest ({elapsed_days}d @ {account.interest_rate}% APR)",
        original_description="Interest",
        source=INTEREST_SOURCE,
        is_cleared=True,
    )
    db.add(tx)
    account.current_balance = account.current_balance + amount
    account.interest_last_accrued_at = now
    await db.flush()
    return tx


async def accrue_due_interest(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[Transaction]:
    """Accrue interest on every eligible active account.

    Returns the list of transactions posted. Skips inactive accounts so that
    archiving an account silently suspends its accrual.
    """
    now = now or datetime.now(timezone.utc)
    stmt = select(Account).where(
        Account.is_active.is_(True),
        Account.interest_rate.isnot(None),
        Account.interest_rate != 0,
    )
    result = await db.execute(stmt)
    accounts = list(result.scalars().all())

    posted: list[Transaction] = []
    for acct in accounts:
        tx = await accrue_interest_for_account(db, acct, now=now)
        if tx is not None:
            posted.append(tx)
    return posted
