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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
# Tagged onto every interest transaction's description (scheduler, retro
# true-up, or manually-entered). This is the identification contract: any
# transaction whose description contains this marker is counted as interest
# when the retro re-evaluation sums "what has actually been posted".
INTEREST_MARKER = "auto a/c interest charge"
DAYS_PER_YEAR = Decimal("365")
CENT = Decimal("0.01")


class InterestNotConfiguredError(Exception):
    """Raised when retro re-evaluation is attempted on an account with no
    interest rate. Caller (router) converts this into a user-facing message."""


@dataclass(frozen=True)
class RetroTxView:
    """Plain-data view of a transaction for the retro modal — decouples the
    template from live ORM objects so the session can be closed before
    rendering without triggering lazy-load errors."""
    id: str
    date: date
    amount: Decimal
    description: str


@dataclass(frozen=True)
class RetroResult:
    """Everything the modal needs to show what just happened.

    All monetary values are signed in account-ledger orientation (positive =
    credit/balance up; negative = debit/balance down). The sign on
    ``delta`` matches the sign of the new transaction posted (if any).

    ``new_transaction`` is ``None`` when the books are already in sync, when
    the account has no history yet, or when the rounded delta is 0.00. The
    modal still renders in those cases so the user sees the math behind
    "no change needed".
    """
    rate: Decimal
    compounding_type_label: str
    compounding_frequency_label: str
    start_date: date
    end_date: date
    num_days: int
    initial_balance: Decimal
    expected_signed: Decimal
    actual_signed: Decimal
    delta: Decimal
    currency: str
    existing_transactions: tuple[RetroTxView, ...] = field(default_factory=tuple)
    new_transaction: RetroTxView | None = None


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
        description=(
            f"Interest {elapsed_days}d @ {account.interest_rate}% APR "
            f"— {INTEREST_MARKER}"
        ),
        original_description=f"Interest — {INTEREST_MARKER}",
        source=INTEREST_SOURCE,
        is_cleared=True,
    )
    db.add(tx)
    account.current_balance = account.current_balance + amount
    account.interest_last_accrued_at = now
    await db.flush()
    return tx


async def retro_reevaluate_interest(
    db: AsyncSession,
    account: Account,
    *,
    now: datetime | None = None,
) -> RetroResult:
    """Retroactively reconcile interest on ``account`` and post a true-up.

    Simulates what interest *should* have accrued from ``account.opened_on``
    up to ``now``, given the account's **current** interest settings applied
    to its actual non-interest transaction history. Compares the simulated
    total to the sum of every existing interest transaction (identified by
    :data:`INTEREST_MARKER` appearing anywhere in ``description``) and posts
    one true-up transaction for the signed difference.

    ``opened_on`` is user-editable, so backdating it lets a long-standing
    real-world account (added to the tracker mid-life) replay its full
    history rather than starting from when the row was inserted.

    Returns a :class:`RetroResult` describing the calculation. The modal
    consumes this directly. ``RetroResult.new_transaction`` is ``None`` when
    the books are already in sync (delta rounds to 0.00) or when the
    account has no completed days to evaluate. Raises
    :class:`InterestNotConfiguredError` when the rate is unset/zero — the
    UI blocks this upstream; the guard here is defensive.

    Simulation rules
    ----------------
    * Whole-day buckets from ``opened_on`` forward, one accrual step per
      completed day (today is not accrued in retro — it'll accrue on the
      next scheduled run).
    * Each day: first apply any non-interest transactions dated on that day
      to the running ``sim_bal``, then accrue.
    * **Compound**: daily growth factor is ``(1 + r/n)^(n/365)`` derived
      from APR + compounding frequency, applied to ``|sim_bal|`` and signed
      by account group. Growth feeds back into ``sim_bal``.
    * **Simple**: daily accrual is ``|sim_bal| * r / 365``, signed by
      group. Does *not* feed back into ``sim_bal``.
    * Pending transactions (``is_pending=True``) are excluded, matching
      :func:`app.services.accounts.recalculate_balance` semantics.

    The retro run does *not* touch ``interest_last_accrued_at``. That
    timestamp belongs to the scheduler's incremental-accrual loop; keeping
    them separate means re-evaluating at any time doesn't disrupt the next
    scheduled run.
    """
    if account.interest_rate is None or account.interest_rate <= 0:
        raise InterestNotConfiguredError(
            f"Account {account.id} has no interest rate configured"
        )

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    end_date = now.date()

    # ``opened_on`` is the user-editable real-world open date and is the
    # correct anchor for the retro window — backdating it lets a
    # long-standing account replay its full history rather than starting
    # from when the row was inserted into our DB.
    start_date = account.opened_on
    if start_date is None:
        # Defensive: column is NOT NULL in the schema, but the in-memory
        # object can be stale right after insert. Refresh and, failing that,
        # fall back to today so the modal explains via num_days==0.
        try:
            await db.refresh(account, ["opened_on"])
        except Exception:  # pragma: no cover — defensive
            pass
        start_date = account.opened_on or end_date

    num_days = (end_date - start_date).days
    if num_days <= 0:
        # Account opened today — no completed days to accrue against, but
        # we still pass the real open date through so the modal can show
        # "opened today" honestly instead of pretending nothing's known
        # about the open date.
        return _empty_result(account, start_date=start_date, end_date=end_date)

    # Fetch ledger: non-interest (by marker) & posted (not pending).
    stmt_non_int = (
        select(Transaction)
        .where(
            Transaction.account_id == account.id,
            Transaction.is_pending.is_(False),
            ~Transaction.description.contains(INTEREST_MARKER),
        )
        .order_by(Transaction.date)
    )
    non_interest = list((await db.execute(stmt_non_int)).scalars().all())

    # Existing interest transactions: match by marker, regardless of pending
    # status or source. The marker is the identification contract.
    stmt_int = (
        select(Transaction)
        .where(
            Transaction.account_id == account.id,
            Transaction.description.contains(INTEREST_MARKER),
        )
        .order_by(Transaction.date)
    )
    interest_txs = list((await db.execute(stmt_int)).scalars().all())
    actual_signed = sum(
        (t.amount for t in interest_txs), Decimal("0.00")
    )

    existing_views = tuple(
        RetroTxView(
            id=str(t.id),
            date=t.date,
            amount=t.amount,
            description=t.description,
        )
        for t in interest_txs
    )

    # Index non-interest transactions by date for O(1) lookup in the loop.
    txs_by_date: dict = {}
    for t in non_interest:
        txs_by_date.setdefault(t.date, []).append(t)

    sign = Decimal(1) if account.group == AccountGroup.ASSET else Decimal(-1)
    rate = Decimal(account.interest_rate) / Decimal("100")
    sim_bal = Decimal(account.initial_balance or 0)
    accrued_total = Decimal(0)

    if account.compounding_type == CompoundingType.COMPOUND:
        n = PERIODS_PER_YEAR[account.compounding_frequency]
        rate_per_period = float(rate) / n
        daily_growth_minus_one = Decimal(
            str((1.0 + rate_per_period) ** (n / 365.0) - 1.0)
        )
    else:
        daily_growth_minus_one = Decimal("0")  # unused

    for i in range(num_days):
        day = start_date + timedelta(days=i)
        for t in txs_by_date.get(day, []):
            sim_bal += t.amount
        principal = abs(sim_bal)
        if principal == 0:
            continue
        if account.compounding_type == CompoundingType.COMPOUND:
            delta_mag = principal * daily_growth_minus_one
            signed_delta = sign * delta_mag
            sim_bal += signed_delta
            accrued_total += signed_delta
        else:  # SIMPLE
            daily_mag = principal * rate / DAYS_PER_YEAR
            accrued_total += sign * daily_mag

    expected_signed = accrued_total.quantize(CENT, rounding=ROUND_HALF_UP)
    actual_signed_q = actual_signed.quantize(CENT, rounding=ROUND_HALF_UP)
    true_up = (expected_signed - actual_signed_q).quantize(
        CENT, rounding=ROUND_HALF_UP
    )

    new_view: RetroTxView | None = None
    if true_up != 0:
        tx = Transaction(
            user_id=account.user_id,
            account_id=account.id,
            date=now.date(),
            amount=true_up,
            description=(
                f"Retro interest true-up ({num_days}d through "
                f"{end_date.isoformat()}) — {INTEREST_MARKER}"
            ),
            original_description=f"Retro interest true-up — {INTEREST_MARKER}",
            source=INTEREST_SOURCE,
            is_cleared=True,
        )
        db.add(tx)
        account.current_balance = account.current_balance + true_up
        await db.flush()
        new_view = RetroTxView(
            id=str(tx.id),
            date=tx.date,
            amount=tx.amount,
            description=tx.description,
        )

    return RetroResult(
        rate=Decimal(account.interest_rate),
        compounding_type_label=account.compounding_type.value.title(),
        compounding_frequency_label=account.compounding_frequency.value.title(),
        start_date=start_date,
        end_date=end_date,
        num_days=num_days,
        initial_balance=Decimal(account.initial_balance or 0),
        expected_signed=expected_signed,
        actual_signed=actual_signed_q,
        delta=true_up,
        currency=account.currency or "",
        existing_transactions=existing_views,
        new_transaction=new_view,
    )


def _empty_result(account: Account, *, start_date: date, end_date: date) -> RetroResult:
    """Result for the early-return cases (no history yet, zero days)."""
    return RetroResult(
        rate=Decimal(account.interest_rate or 0),
        compounding_type_label=account.compounding_type.value.title(),
        compounding_frequency_label=account.compounding_frequency.value.title(),
        start_date=start_date,
        end_date=end_date,
        num_days=0,
        initial_balance=Decimal(account.initial_balance or 0),
        expected_signed=Decimal("0.00"),
        actual_signed=Decimal("0.00"),
        delta=Decimal("0.00"),
        currency=account.currency or "",
        existing_transactions=(),
        new_transaction=None,
    )


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
