"""Build the data for a user-facing printable account statement.

Distinct from :mod:`app.models.statement` (imported file-tracking) and from
reconciliation: this module assembles a read-only, point-in-time packet that
the ``/statements`` router renders as a print-optimised HTML page. The browser
print dialog is what actually produces the PDF — we don't own the paginator,
so we do a tiny bit of our own "virtual pagination" to emit NZ-style
brought-forward / carried-forward rows at deterministic row counts.

The service is pure computation: it reads the same posted-transaction set
that :func:`app.services.accounts.recalculate_balance` uses (``is_pending=False``)
so the closing balance always reconciles with what the rest of the app shows.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User


# Row count per paginated chunk inside a single account's detail. Chosen to fit
# an A4 portrait page at ~14px line-height with the headers we render. Users can
# override via the ``rows_per_page`` query param if their printer's margins
# differ — the brought-forward/carried-forward scaffolding adapts automatically.
DEFAULT_ROWS_PER_PAGE = 25

_ZERO = Decimal("0.00")

# Category badge palette. Mirrors the 15-colour set used in dashboard/index.html
# and reports/deepdive.html so a category that appears blue on the dashboard
# reads as the same blue on the printed statement. Colours are assigned to
# categories deterministically within a packet (see _build_category_palette),
# so reprints of the same period use the same colours.
CATEGORY_PALETTE: tuple[str, ...] = (
    "#6366f1", "#f59e0b", "#10b981", "#ef4444", "#3b82f6",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#64748b",
    "#a855f7", "#06b6d4", "#e11d48", "#84cc16", "#78716c",
)


# ---------------------------------------------------------------------------
# Dataclasses — the view-model the template iterates over.
# ---------------------------------------------------------------------------


@dataclass
class StatementCategoryTag:
    """Lightweight view-model for a category badge on a transaction row.

    We deliberately do NOT expose the full ``Category`` ORM object to the
    template — the statement view should only ever see the fields it needs
    (name, colour, and a short legend number) and should not accidentally
    trigger lazy loads during render (the page is produced outside the
    request's usual SQLAlchemy session lifecycle in some test paths).

    ``number`` is a 1-based sequential index assigned *after* the legend is
    assembled, so numbering reflects only categories actually present in
    the statement (no gaps). Transaction rows render just this number as a
    coloured chip, keeping every chip the same width regardless of category
    name length; the name-to-number mapping lives in the legend on page 1.
    """

    id: uuid.UUID
    name: str
    color: str
    number: int = 0


@dataclass
class StatementTxn:
    """A single posted transaction row with its running balance."""

    date: date
    description: str
    reference: str | None
    amount: Decimal
    running_balance: Decimal
    category: StatementCategoryTag | None = None

    @property
    def is_credit(self) -> bool:
        return self.amount > 0

    @property
    def is_debit(self) -> bool:
        return self.amount < 0

    @property
    def credit(self) -> Decimal:
        return self.amount if self.amount > 0 else _ZERO

    @property
    def debit(self) -> Decimal:
        """Debit column value — stored as a positive magnitude for display."""
        return -self.amount if self.amount < 0 else _ZERO


@dataclass
class StatementPage:
    """A virtual page inside one account's transaction detail.

    Carries the brought-forward balance shown at the top of the page (the
    running balance *before* the first row on this page) and the
    carried-forward balance shown at the bottom (the running balance *after*
    the last row on this page). On the first page these collapse to the
    opening / last-row balance respectively; on the last page the
    carried-forward value equals the closing balance.
    """

    page_index: int          # 0-based within the account
    total_pages: int         # total pages for this account
    brought_forward: Decimal
    carried_forward: Decimal
    transactions: list[StatementTxn]

    @property
    def is_first(self) -> bool:
        return self.page_index == 0

    @property
    def is_last(self) -> bool:
        return self.page_index == self.total_pages - 1


@dataclass
class AccountStatement:
    """Per-account portion of the statement."""

    account: Account
    opening_balance: Decimal
    closing_balance: Decimal
    total_credits: Decimal
    total_debits: Decimal     # positive magnitude
    txn_count: int
    pages: list[StatementPage] = field(default_factory=list)

    @property
    def net_movement(self) -> Decimal:
        """Closing minus opening — always reconciles to credits - debits."""
        return self.closing_balance - self.opening_balance

    @property
    def currency(self) -> str:
        return self.account.currency or "NZD"


@dataclass
class StatementPacket:
    """The top-level object the template consumes."""

    user: User
    start_date: date
    end_date: date
    generated_at: datetime
    accounts: list[AccountStatement]

    # Per-currency rollups for the summary page. Mixed-currency statements
    # need separate totals rows — we deliberately do NOT sum across
    # currencies because there is no FX conversion in this app.
    summary_by_currency: dict[str, dict[str, Decimal]] = field(default_factory=dict)

    # Unique statement reference, useful on print so reprints line up.
    reference: str = ""

    # Ordered list of (category name, colour) pairs actually used in this
    # packet — used to render a compact "Category legend" box on the summary
    # sheet so a reader can decode the coloured pills on the detail pages
    # without having to open the app.
    category_legend: list[StatementCategoryTag] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_statement(
    db: AsyncSession,
    user: User,
    account_ids: Iterable[uuid.UUID],
    start_date: date,
    end_date: date,
    rows_per_page: int = DEFAULT_ROWS_PER_PAGE,
) -> StatementPacket:
    """Assemble a :class:`StatementPacket` for the given accounts / range.

    Arguments
    ---------
    db
        Active async session.
    user
        Account holder — used purely for the header / ownership check.
    account_ids
        Account UUIDs to include. Any IDs that don't belong to ``user`` are
        silently dropped; this is a belt-and-braces check since the router
        already filters, but keeping it here means the service is safe for
        direct use (e.g. CLI or test fixtures).
    start_date, end_date
        Inclusive bounds. ``opening_balance`` is computed from transactions
        strictly before ``start_date``; ``closing_balance`` covers everything
        up to and including ``end_date``.
    rows_per_page
        Target rows per virtual page inside an account's detail. Minimum 5 to
        guarantee the brought/carried-forward rows always have company.
    """
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    rows_per_page = max(5, rows_per_page)

    # Filter to user-owned accounts, preserve the user-specified order
    # when possible (sort_order then name).
    id_list = list(account_ids)
    if not id_list:
        raise ValueError("At least one account must be selected")

    result = await db.execute(
        select(Account)
        .where(Account.user_id == user.id, Account.id.in_(id_list))
        .order_by(Account.sort_order, Account.name)
    )
    accounts = list(result.scalars().all())

    # Pre-scan all categories used across the selected accounts+range so we
    # can assign every category a stable palette colour up-front. Doing this
    # in one pass (rather than inside each account's build) means:
    #   * the palette cycles through categories in a stable order (alpha),
    #     so "Groceries" is the same colour regardless of which account's
    #     detail page you're looking at;
    #   * the summary sheet can render a legend that reflects the same
    #     mapping.
    palette_map = await _build_category_palette(db, user, account_ids=list(id_list))

    statements: list[AccountStatement] = []
    for acct in accounts:
        statements.append(
            await _build_account_statement(
                db, acct, start_date, end_date, rows_per_page, palette_map,
            )
        )

    summary_by_currency = _summarise_by_currency(statements)

    # Build the legend strictly from categories that actually appear in the
    # produced rows, not from everything in the palette_map — users with a
    # large category tree shouldn't see a wall of unused colours.
    legend = _collect_legend(statements, palette_map)

    # Assign 1-based legend numbers to the tags in-place. Since the same
    # StatementCategoryTag instance is shared between the legend and every
    # transaction that references it (via palette_map lookup), mutating
    # ``number`` here propagates automatically to the txn rows — no need to
    # thread the numbering through the row-building loop.
    for i, tag in enumerate(legend, start=1):
        tag.number = i

    return StatementPacket(
        user=user,
        start_date=start_date,
        end_date=end_date,
        generated_at=datetime.now(timezone.utc),
        accounts=statements,
        summary_by_currency=summary_by_currency,
        reference=_build_reference(user.id, start_date, end_date),
        category_legend=legend,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _opening_balance(
    db: AsyncSession, account: Account, before: date,
) -> Decimal:
    """Balance at the close of ``before - 1 day`` — the statement's starting point.

    Matches :func:`app.services.accounts.recalculate_balance` in excluding
    pending transactions. Pending items are shown separately in the bank-feed
    reconciliation view; including them here would conflict with that tool's
    "unreconciled" figure and with the account's own ``current_balance``.
    """
    result = await db.execute(
        select(sa_func.coalesce(sa_func.sum(Transaction.amount), _ZERO))
        .where(
            Transaction.account_id == account.id,
            Transaction.is_pending.is_(False),
            Transaction.date < before,
        )
    )
    tx_sum = result.scalar() or _ZERO
    if not isinstance(tx_sum, Decimal):
        tx_sum = Decimal(str(tx_sum))
    return (account.initial_balance or _ZERO) + tx_sum


async def _period_transactions(
    db: AsyncSession, account: Account, start: date, end: date,
) -> list[Transaction]:
    """Posted transactions in [start, end] ordered by (date, created_at).

    Using ``created_at`` as a stable tiebreaker keeps the running balance
    deterministic across runs — important because the summary page's closing
    balance has to exactly equal the final row's running balance.

    Eager-loads ``category`` so the template can render category pills
    without triggering lazy loads mid-render (the statement is occasionally
    rendered outside a live session e.g. in tests / preview scripts).
    """
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.account_id == account.id,
            Transaction.is_pending.is_(False),
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .options(selectinload(Transaction.category))
        .order_by(Transaction.date.asc(), Transaction.created_at.asc())
    )
    return list(result.scalars().all())


async def _build_category_palette(
    db: AsyncSession,
    user: User,
    account_ids: list[uuid.UUID],
) -> dict[uuid.UUID, StatementCategoryTag]:
    """Assign a stable palette colour to every user category.

    We don't restrict to categories that actually appear in the period — it's
    cheap to fetch the full set, and doing so means that a user who runs
    overlapping statements over several periods gets the same colour for
    "Groceries" every time. Colours cycle when the category count exceeds
    the palette length; that's fine because the legend on the summary page
    disambiguates by name.

    Ordering is alphabetical (case-insensitive) for predictability — it means
    the first five colours in the palette tend to map to the same categories
    across reprints rather than jittering with insertion order.
    """
    result = await db.execute(
        select(Category).where(Category.user_id == user.id)
    )
    cats = list(result.scalars().all())
    cats.sort(key=lambda c: (c.name.lower(), str(c.id)))
    palette: dict[uuid.UUID, StatementCategoryTag] = {}
    for idx, c in enumerate(cats):
        palette[c.id] = StatementCategoryTag(
            id=c.id,
            name=c.name,
            color=CATEGORY_PALETTE[idx % len(CATEGORY_PALETTE)],
        )
    return palette


def _collect_legend(
    statements: list[AccountStatement],
    palette_map: dict[uuid.UUID, StatementCategoryTag],
) -> list[StatementCategoryTag]:
    """Pull the distinct category tags that appear in any rendered row.

    Preserves palette order (which was alpha-by-name) rather than
    first-seen order, so the legend reads the same on reprint.
    """
    seen: set[uuid.UUID] = set()
    for s in statements:
        for page in s.pages:
            for tx in page.transactions:
                if tx.category is not None:
                    seen.add(tx.category.id)
    # Emit in palette iteration order (alpha) so the legend is stable.
    return [tag for cat_id, tag in palette_map.items() if cat_id in seen]


async def _build_account_statement(
    db: AsyncSession,
    account: Account,
    start: date,
    end: date,
    rows_per_page: int,
    palette_map: dict[uuid.UUID, StatementCategoryTag] | None = None,
) -> AccountStatement:
    opening = await _opening_balance(db, account, start)
    txs = await _period_transactions(db, account, start, end)

    palette_map = palette_map or {}

    rows: list[StatementTxn] = []
    running = opening
    total_credits = _ZERO
    total_debits = _ZERO
    for tx in txs:
        running = running + tx.amount
        if tx.amount > 0:
            total_credits += tx.amount
        else:
            total_debits += -tx.amount
        cat_tag: StatementCategoryTag | None = None
        if tx.category_id is not None:
            # Prefer the palette-assigned tag (stable colour); fall back to
            # a locally-built tag if the category was created between the
            # palette pre-scan and the row read (rare — but keeps rendering
            # robust).
            cat_tag = palette_map.get(tx.category_id)
            if cat_tag is None and tx.category is not None:
                cat_tag = StatementCategoryTag(
                    id=tx.category.id,
                    name=tx.category.name,
                    color=CATEGORY_PALETTE[0],
                )
        rows.append(
            StatementTxn(
                date=tx.date,
                description=tx.description,
                reference=tx.reference,
                amount=tx.amount,
                running_balance=running,
                category=cat_tag,
            )
        )

    closing = running  # final running == closing (equal to opening when empty)

    pages = _paginate(rows, opening, closing, rows_per_page)

    return AccountStatement(
        account=account,
        opening_balance=opening,
        closing_balance=closing,
        total_credits=total_credits,
        total_debits=total_debits,
        txn_count=len(rows),
        pages=pages,
    )


def _paginate(
    rows: list[StatementTxn],
    opening: Decimal,
    closing: Decimal,
    per_page: int,
) -> list[StatementPage]:
    """Slice the row list into virtual pages with brought/carried-forward
    scaffolding.

    An empty-period statement still emits a single page (no rows) so the
    template has a stable loop shape — the page shows opening == closing and
    a "No transactions in this period" placeholder.
    """
    if not rows:
        return [
            StatementPage(
                page_index=0,
                total_pages=1,
                brought_forward=opening,
                carried_forward=closing,
                transactions=[],
            )
        ]

    chunks: list[list[StatementTxn]] = [
        rows[i : i + per_page] for i in range(0, len(rows), per_page)
    ]
    total = len(chunks)
    pages: list[StatementPage] = []
    for idx, chunk in enumerate(chunks):
        # brought-forward = running balance entering the first row of this chunk
        # = running balance *after* the previous chunk's last row, which is
        # opening if idx == 0 else the previous chunk's tail.
        if idx == 0:
            bf = opening
        else:
            bf = chunks[idx - 1][-1].running_balance
        cf = chunk[-1].running_balance
        pages.append(
            StatementPage(
                page_index=idx,
                total_pages=total,
                brought_forward=bf,
                carried_forward=cf,
                transactions=chunk,
            )
        )
    return pages


def _summarise_by_currency(
    statements: list[AccountStatement],
) -> dict[str, dict[str, Decimal]]:
    """Aggregate opening/closing/credits/debits per currency."""
    summary: dict[str, dict[str, Decimal]] = {}
    for s in statements:
        bucket = summary.setdefault(
            s.currency,
            {
                "opening": _ZERO,
                "closing": _ZERO,
                "credits": _ZERO,
                "debits": _ZERO,
                "accounts": 0,
            },
        )
        bucket["opening"] += s.opening_balance
        bucket["closing"] += s.closing_balance
        bucket["credits"] += s.total_credits
        bucket["debits"] += s.total_debits
        bucket["accounts"] = int(bucket.get("accounts", 0)) + 1
    return summary


def _build_reference(user_id: uuid.UUID, start: date, end: date) -> str:
    """Compact reference shown in the header — ``<userhash>-YYMMDD-YYMMDD``.

    Stable for a (user, period) pair; handy when reprinting a run and wanting
    to confirm the second copy matches the first.
    """
    short = str(user_id).split("-", 1)[0].upper()
    return f"{short}-{start.strftime('%y%m%d')}-{end.strftime('%y%m%d')}"
