"""Akahu bank-feed API client and sync logic.

Uses the Akahu Personal App API (https://developers.akahu.nz/docs/personal-apps).
Auth headers:
    Authorization: Bearer {AKAHU_USER_TOKEN}
    X-Akahu-Id:    {AKAHU_APP_TOKEN}
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dates import parse_iso_datetime_or_none
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.categoriser import suggest_category
from app.services.accounts import recalculate_balance

log = logging.getLogger(__name__)

NZTZ = ZoneInfo("Pacific/Auckland")
AKAHU_SOURCE = "akahu"

SYNC_MUTABLE_FIELDS = frozenset(
    {"date", "amount", "description", "reference", "akahu_updated_at", "is_pending"}
)

# Akahu indexes transactions by their *transaction date* (when the transaction
# took place). Banks, however, post weekend/after-hours transactions to a
# statement on the next business day, so a single statement line can carry a
# date 1-2 business days after Akahu's transaction date — and Akahu exposes no
# separate posting/settlement date to bridge the gap. When a sync window is
# anchored to a statement period, a boundary transaction whose Akahu date sits
# just outside the requested range would never be fetched, silently dropping a
# statement line. Pad the fetch window by this many days on each side so those
# boundary rows are imported. They are real, correctly-dated transactions; they
# simply also surface on the adjacent days.
FETCH_WINDOW_PAD_DAYS = 5


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AkahuConfigError(Exception):
    """Raised when Akahu credentials are missing."""


class AkahuAPIError(Exception):
    """Raised on non-2xx Akahu responses."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Akahu API {status_code}: {message}")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def nz_date_to_utc_range(start_date: date, end_date: date) -> tuple[str, str]:
    """Convert NZ-local date boundaries to UTC ISO 8601 strings for Akahu.

    start_date -> start of day in Pacific/Auckland
    end_date   -> end of day (23:59:59.999) in Pacific/Auckland
    """
    start_nz = datetime.combine(start_date, time.min, tzinfo=NZTZ)
    end_nz = datetime.combine(
        end_date, time(23, 59, 59, 999_000), tzinfo=NZTZ
    )
    start_utc = start_nz.astimezone(timezone.utc)
    end_utc = end_nz.astimezone(timezone.utc)
    return (
        start_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        end_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    )


def _shift_utc_iso(raw: str, days: int) -> str:
    """Shift a UTC ISO 8601 timestamp by ``days`` and re-serialise it.

    Used to widen the Akahu fetch window (see ``FETCH_WINDOW_PAD_DAYS``). Falls
    back to returning ``raw`` unchanged if it can't be parsed, so a malformed
    bound never aborts a sync.
    """
    dt = parse_iso_datetime_or_none(raw)
    if dt is None:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    shifted = dt.astimezone(timezone.utc) + timedelta(days=days)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_iso_to_nz_date(raw: str | None, fallback: date | None) -> date | None:
    """Convert an Akahu UTC timestamp string to its NZ-local calendar date.

    Akahu returns transaction timestamps in UTC. Taking the UTC ``.date()``
    directly shifts NZ-morning transactions back a day (NZ is UTC+12/+13), so a
    transaction posted on the 18th NZ time would be stored as the 17th. Convert
    into Pacific/Auckland before extracting the date so stored dates match what
    the bank statement shows. ``fallback`` is returned for missing/malformed
    input.
    """
    dt = parse_iso_datetime_or_none(raw)
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NZTZ).date()


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------

def _check_config() -> None:
    if not settings.AKAHU_APP_TOKEN or not settings.AKAHU_USER_TOKEN:
        raise AkahuConfigError(
            "Akahu credentials not configured. "
            "Set AKAHU_APP_TOKEN and AKAHU_USER_TOKEN in .env"
        )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.AKAHU_USER_TOKEN}",
        "X-Akahu-Id": settings.AKAHU_APP_TOKEN,
        "Accept": "application/json",
    }


def _base_url() -> str:
    return settings.AKAHU_BASE_URL.rstrip("/")


async def _akahu_get(path: str, params: dict | None = None) -> dict:
    _check_config()
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=_headers(), params=params)
    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg = body.get("message", resp.text[:200])
        raise AkahuAPIError(resp.status_code, msg)
    data = resp.json()
    if not data.get("success"):
        raise AkahuAPIError(resp.status_code, data.get("message", "Unknown error"))
    return data


def is_configured() -> bool:
    return bool(settings.AKAHU_APP_TOKEN and settings.AKAHU_USER_TOKEN)


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

async def fetch_accounts() -> list[dict]:
    """Fetch all connected Akahu accounts."""
    data = await _akahu_get("/accounts")
    return data.get("items", [])


async def fetch_account_transactions(
    akahu_account_id: str,
    start_utc: str,
    end_utc: str,
) -> list[dict]:
    """Fetch settled transactions for one account, paginating until exhausted.

    Uses the account-specific endpoint: GET /accounts/{id}/transactions
    Keeps the same start/end on every cursor request.
    """
    all_items: list[dict] = []
    params: dict[str, str] = {"start": start_utc, "end": end_utc}

    while True:
        data = await _akahu_get(
            f"/accounts/{akahu_account_id}/transactions", params=params
        )
        items = data.get("items", [])
        all_items.extend(items)

        cursor_next = (data.get("cursor") or {}).get("next")
        if not cursor_next or not items:
            break
        params = {"start": start_utc, "end": end_utc, "cursor": cursor_next}

    return all_items


async def fetch_pending_transactions() -> list[dict]:
    """Fetch currently-pending transactions across all connected accounts.

    Uses GET /transactions/pending which returns every pending (authorised but
    not yet settled) transaction the user token can see. Callers are expected
    to partition by ``_account`` downstream.
    """
    data = await _akahu_get("/transactions/pending")
    return data.get("items", [])


# ---------------------------------------------------------------------------
# Akahu -> local type mapping
# ---------------------------------------------------------------------------

_AKAHU_TYPE_MAP = {
    "CHECKING": "checking",
    "SAVINGS": "savings",
    "CREDITCARD": "credit_card",
    "LOAN": "loan",
    "INVESTMENT": "investment",
    "KIWISAVER": "investment",
    "TERMDEPOSIT": "savings",
    "FOREIGN": "other",
    "TAX": "other",
    "REWARDS": "other",
    "WALLET": "other",
}


def akahu_account_type(akahu_type: str) -> str:
    return _AKAHU_TYPE_MAP.get(akahu_type, "other")


# ---------------------------------------------------------------------------
# Balance sync
# ---------------------------------------------------------------------------

async def sync_account_balances(
    db: AsyncSession, user_id: uuid.UUID
) -> dict:
    """Fetch Akahu account balances and store them as the *reported* balance.

    The bank-reported balance is the authoritative "where am I right now"
    number. We keep it in ``Account.reported_balance`` separately from
    ``current_balance`` (which is transaction-derived). Reports can then show
    both and surface the delta honestly rather than silently flip-flopping
    a single column on every sync.

    Idempotent: skips write when both the value and timestamp are unchanged.
    """
    result = {
        "linked_found": 0,
        "updated": 0,
        "unchanged": 0,
        "missing_in_akahu": 0,
        "errors": [],
    }

    try:
        akahu_accounts = await fetch_accounts()
    except (AkahuConfigError, AkahuAPIError) as exc:
        result["errors"].append(str(exc))
        return result

    akahu_by_id: dict[str, dict] = {a["_id"]: a for a in akahu_accounts}

    stmt = select(Account).where(
        Account.user_id == user_id,
        Account.akahu_id.isnot(None),
    )
    rows = await db.execute(stmt)
    linked_accounts = list(rows.scalars().all())
    result["linked_found"] = len(linked_accounts)

    for acct in linked_accounts:
        akahu_acct = akahu_by_id.get(acct.akahu_id)
        if not akahu_acct:
            result["missing_in_akahu"] += 1
            continue

        try:
            balance_data = akahu_acct.get("balance", {})
            new_balance = Decimal(str(balance_data.get("current", 0)))
        except (InvalidOperation, TypeError) as exc:
            result["errors"].append(f"{acct.name}: bad balance value ({exc})")
            continue

        # Akahu surfaces its own last-refresh timestamps under ``refreshed``.
        # That's the moment the *bank* told Akahu the balance — what the user
        # cares about for staleness — distinct from our own sync clock.
        refreshed = akahu_acct.get("refreshed") or {}
        reported_as_of = parse_iso_datetime_or_none(refreshed.get("balance"))

        changed = False
        if acct.reported_balance != new_balance:
            acct.reported_balance = new_balance
            changed = True
        if reported_as_of and acct.reported_balance_as_of != reported_as_of:
            acct.reported_balance_as_of = reported_as_of
            changed = True

        if changed:
            result["updated"] += 1
            log.info(
                "Reported balance updated: %s -> %s (as of %s)",
                acct.name, new_balance, reported_as_of,
            )
        else:
            result["unchanged"] += 1

    await db.flush()
    return result


# ---------------------------------------------------------------------------
# Transaction sync
# ---------------------------------------------------------------------------

def _parse_akahu_tx(
    raw: dict,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    is_pending: bool = False,
) -> dict:
    """Parse a raw Akahu transaction dict into local Transaction field values.

    ``is_pending`` is provided by the caller based on which endpoint the row
    came from (the posted endpoint always yields ``False``; the pending
    endpoint always yields ``True``). Akahu does not put a flag on the row
    itself — the endpoint is the source of truth.
    """
    meta = raw.get("meta") or {}
    tx_date = utc_iso_to_nz_date(raw.get("date"), date.today())

    try:
        amount = Decimal(str(raw.get("amount", 0)))
    except InvalidOperation:
        amount = Decimal("0.00")

    akahu_updated_at = parse_iso_datetime_or_none(raw.get("updated_at"))

    return {
        "user_id": user_id,
        "account_id": account_id,
        "date": tx_date,
        "amount": amount,
        "description": raw.get("description", "")[:500],
        "original_description": raw.get("description", "")[:500],
        "reference": meta.get("reference", "")[:100] or None,
        "source": AKAHU_SOURCE,
        "akahu_transaction_id": raw["_id"],
        "akahu_account_id": raw.get("_account", ""),
        "akahu_updated_at": akahu_updated_at,
        "is_pending": is_pending,
    }


async def sync_account_transactions(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    start_utc: str,
    end_utc: str,
) -> dict:
    """Sync settled Akahu transactions for one linked account.

    Upserts by (source, akahu_transaction_id). Marks disappeared rows as stale.
    Never overwrites user-managed fields (category_id, notes, is_cleared,
    original_description).
    """
    result = {
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "stale_marked": 0,
        "stale_cleared": 0,
        "errors": [],
    }

    acct = await db.get(Account, account_id)
    if not acct or acct.user_id != user_id:
        result["errors"].append("Account not found or access denied")
        return result
    if not acct.akahu_id:
        result["errors"].append("Account is not linked to an Akahu account")
        return result

    # Widen the requested window so transactions whose Akahu transaction-date
    # falls just outside the (statement-anchored) range are still imported.
    # See FETCH_WINDOW_PAD_DAYS for the why. The stale-detection range below is
    # derived from these same padded bounds so it stays consistent with exactly
    # what we fetched.
    fetch_start_utc = _shift_utc_iso(start_utc, -FETCH_WINDOW_PAD_DAYS)
    fetch_end_utc = _shift_utc_iso(end_utc, FETCH_WINDOW_PAD_DAYS)

    try:
        raw_txs = await fetch_account_transactions(
            acct.akahu_id, fetch_start_utc, fetch_end_utc
        )
    except (AkahuConfigError, AkahuAPIError) as exc:
        result["errors"].append(str(exc))
        return result

    result["fetched"] = len(raw_txs)
    seen_akahu_ids: set[str] = set()

    for raw in raw_txs:
        akahu_tx_id = raw.get("_id")
        if not akahu_tx_id:
            continue
        seen_akahu_ids.add(akahu_tx_id)

        parsed = _parse_akahu_tx(raw, account_id, user_id)

        # Each row is upserted inside its own SAVEPOINT so a single bad row
        # (e.g. a constraint violation) is isolated and reported instead of
        # rolling back the entire batch and losing every other transaction.
        # Counters are only applied once the savepoint commits cleanly.
        try:
            async with db.begin_nested():
                existing_result = await db.execute(
                    select(Transaction).where(
                        Transaction.source == AKAHU_SOURCE,
                        Transaction.akahu_transaction_id == akahu_tx_id,
                    )
                )
                existing: Transaction | None = existing_result.scalar_one_or_none()

                cleared_stale = False
                if existing is None:
                    category_id = await suggest_category(db, user_id, parsed["description"])
                    db.add(Transaction(**parsed, category_id=category_id))
                    outcome = "inserted"
                else:
                    changed = False
                    for field in SYNC_MUTABLE_FIELDS:
                        new_val = parsed.get(field)
                        old_val = getattr(existing, field, None)
                        if new_val != old_val:
                            setattr(existing, field, new_val)
                            changed = True

                    if existing.is_source_stale:
                        existing.is_source_stale = False
                        existing.source_stale_since = None
                        cleared_stale = True
                        changed = True

                    outcome = "updated" if changed else "unchanged"

                # Force constraint evaluation now, while we're inside the
                # savepoint, so any IntegrityError is caught per-row below.
                await db.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))[:200]
            result["errors"].append(
                f"Skipped transaction {akahu_tx_id} "
                f"({parsed['description'][:60]!r}): {detail}"
            )
            log.warning("Akahu posted sync skipped row %s: %s", akahu_tx_id, exc)
            continue

        result[outcome] += 1
        if cleared_stale:
            result["stale_cleared"] += 1

    # --- stale detection ---
    # Convert the UTC range bounds back to NZ-local dates so they line up with
    # the NZ-local dates we store on each transaction (see utc_iso_to_nz_date).
    range_start = utc_iso_to_nz_date(fetch_start_utc, None)
    range_end = utc_iso_to_nz_date(fetch_end_utc, None)

    # Stale detection only applies to *posted* rows — pending rows have their
    # own lifecycle (see ``sync_account_pending_transactions``) and must not be
    # tombstoned by the posted sync when they're simply not yet settled.
    if range_start and range_end and seen_akahu_ids:
        stale_stmt = select(Transaction).where(
            Transaction.source == AKAHU_SOURCE,
            Transaction.akahu_account_id == acct.akahu_id,
            Transaction.account_id == account_id,
            Transaction.date >= range_start,
            Transaction.date <= range_end,
            Transaction.is_pending.is_(False),
            Transaction.is_source_stale.is_(False),
            Transaction.akahu_transaction_id.notin_(seen_akahu_ids),
        )
        stale_result = await db.execute(stale_stmt)
        for stale_tx in stale_result.scalars():
            stale_tx.is_source_stale = True
            stale_tx.source_stale_since = sa_func.now()
            result["stale_marked"] += 1
    elif range_start and range_end and not seen_akahu_ids:
        stale_stmt = select(Transaction).where(
            Transaction.source == AKAHU_SOURCE,
            Transaction.akahu_account_id == acct.akahu_id,
            Transaction.account_id == account_id,
            Transaction.date >= range_start,
            Transaction.date <= range_end,
            Transaction.is_pending.is_(False),
            Transaction.is_source_stale.is_(False),
        )
        stale_result = await db.execute(stale_stmt)
        for stale_tx in stale_result.scalars():
            stale_tx.is_source_stale = True
            stale_tx.source_stale_since = sa_func.now()
            result["stale_marked"] += 1

    # Record that we've successfully pulled posted transactions for this
    # account so the reports UI can show the transaction-feed freshness.
    acct.transactions_as_of = datetime.now(timezone.utc)

    await db.flush()
    await recalculate_balance(db, account_id)

    return result


# ---------------------------------------------------------------------------
# Pending transaction sync
# ---------------------------------------------------------------------------

async def sync_account_pending_transactions(
    db: AsyncSession,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
) -> dict:
    """Refresh the set of *pending* transactions for one or all linked accounts.

    Pending rows live in the same ``transactions`` table with
    ``is_pending=True``. On every call we (a) upsert every row returned from
    ``/transactions/pending`` and (b) delete any local pending rows that no
    longer appear — Akahu removes a transaction from the pending endpoint the
    moment it posts (at which point the posted sync picks it up under a
    different ID and the stale-pending is already gone).

    Pending rows are excluded from balance recalculation and from category
    aggregations so they never distort reports or budgets. They show up only
    as a clearly-badged layer on top of posted data.
    """
    result = {
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "errors": [],
    }

    stmt = select(Account).where(
        Account.user_id == user_id,
        Account.akahu_id.isnot(None),
    )
    if account_id is not None:
        stmt = stmt.where(Account.id == account_id)
    linked = list((await db.execute(stmt)).scalars().all())
    if not linked:
        return result

    akahu_id_to_local: dict[str, Account] = {a.akahu_id: a for a in linked}

    try:
        raw_pending = await fetch_pending_transactions()
    except (AkahuConfigError, AkahuAPIError) as exc:
        result["errors"].append(str(exc))
        return result

    # Partition by Akahu account ID, keeping only the ones we care about.
    by_account: dict[str, list[dict]] = {aid: [] for aid in akahu_id_to_local}
    for raw in raw_pending:
        aid = raw.get("_account")
        if aid in by_account:
            by_account[aid].append(raw)

    result["fetched"] = sum(len(v) for v in by_account.values())

    for akahu_acct_id, rows in by_account.items():
        acct = akahu_id_to_local[akahu_acct_id]
        seen: set[str] = set()

        for raw in rows:
            akahu_tx_id = raw.get("_id")
            if not akahu_tx_id:
                continue
            seen.add(akahu_tx_id)
            parsed = _parse_akahu_tx(raw, acct.id, user_id, is_pending=True)

            existing = (await db.execute(
                select(Transaction).where(
                    Transaction.source == AKAHU_SOURCE,
                    Transaction.akahu_transaction_id == akahu_tx_id,
                )
            )).scalar_one_or_none()

            if existing is None:
                category_id = await suggest_category(db, user_id, parsed["description"])
                db.add(Transaction(**parsed, category_id=category_id))
                result["inserted"] += 1
            else:
                changed = False
                for field in SYNC_MUTABLE_FIELDS:
                    new_val = parsed.get(field)
                    old_val = getattr(existing, field, None)
                    if new_val != old_val:
                        setattr(existing, field, new_val)
                        changed = True
                if changed:
                    result["updated"] += 1
                else:
                    result["unchanged"] += 1

        # Any previously-pending row for this account that no longer appears
        # in the pending feed has either posted (we'll pick it up fresh under
        # its posted ID) or been cancelled. Either way, drop the pending row.
        gone_stmt = select(Transaction).where(
            Transaction.source == AKAHU_SOURCE,
            Transaction.account_id == acct.id,
            Transaction.is_pending.is_(True),
        )
        if seen:
            gone_stmt = gone_stmt.where(
                Transaction.akahu_transaction_id.notin_(seen)
            )
        to_remove = list((await db.execute(gone_stmt)).scalars().all())
        for tx in to_remove:
            await db.delete(tx)
            result["removed"] += 1

    await db.flush()
    return result
