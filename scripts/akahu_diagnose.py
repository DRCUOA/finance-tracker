"""Diagnose missing Akahu bank-feed transactions.

Calls the Akahu API directly for a date range and prints every transaction it
returns for each linked account, alongside whether that transaction already
exists in the local DB. This isolates *where* transactions go missing:

  * If a statement transaction is NOT in the Akahu output  -> Akahu isn't
    returning it (wrong account, data-depth limit, still-pending, or the date
    window doesn't cover it). The app can't import what it never receives.
  * If it IS in the Akahu output but NOT in the DB          -> our insert path
    is dropping it; dig into sync_account_transactions.

Read-only. Makes no changes to the DB or to Akahu.

Usage:
    source .venv/bin/activate
    PYTHONPATH=. python scripts/akahu_diagnose.py START END [ACCOUNT_NAME_SUBSTR]

    START / END are NZ-local ISO dates (YYYY-MM-DD), same as the sync form.
    ACCOUNT_NAME_SUBSTR optionally restricts to accounts whose name contains it.

Example:
    PYTHONPATH=. python scripts/akahu_diagnose.py 2026-02-15 2026-04-14 "Home Loan"
"""

import asyncio
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
# Import every model module so SQLAlchemy's mapper registry is fully populated
# before we run any ORM query. The relationships on Account/Transaction
# reference User, Category, etc. by name, and string-based relationships only
# resolve once those classes have been imported.
from app.models import (  # noqa: F401
    account,
    budget,
    category,
    commitment,
    reconciliation,
    statement,
    transaction,
    user,
)
from app.models.account import Account
from app.models.transaction import Transaction
from app.services.akahu import (
    AKAHU_SOURCE,
    AkahuAPIError,
    AkahuConfigError,
    fetch_account_transactions,
    fetch_accounts,
    is_configured,
    nz_date_to_utc_range,
    utc_iso_to_nz_date,
)
from app.dates import parse_iso_date


async def run(start: date, end: date, name_filter: str | None) -> None:
    if not is_configured():
        print("Akahu credentials not configured (AKAHU_APP_TOKEN / AKAHU_USER_TOKEN).")
        return

    start_utc, end_utc = nz_date_to_utc_range(start, end)
    print(f"NZ range : {start.isoformat()} -> {end.isoformat()}")
    print(f"UTC range: {start_utc} -> {end_utc}\n")

    # What does Akahu think this connection has? Useful if the wrong account is
    # linked, or an account silently dropped out of the connection.
    try:
        akahu_accounts = await fetch_accounts()
    except (AkahuConfigError, AkahuAPIError) as exc:
        print(f"Failed to list Akahu accounts: {exc}")
        return

    print(f"Akahu reports {len(akahu_accounts)} connected account(s):")
    for a in akahu_accounts:
        print(f"  {a.get('_id'):<28s}  {a.get('name', '?')!r}  "
              f"({(a.get('connection') or {}).get('name', '?')})")
    print()

    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        stmt = select(Account).where(Account.akahu_id.isnot(None))
        if name_filter:
            stmt = stmt.where(Account.name.ilike(f"%{name_filter}%"))
        local_accounts = list((await db.execute(stmt)).scalars().all())

        if not local_accounts:
            print("No linked local accounts matched. Nothing to diagnose.")
            await engine.dispose()
            return

        for acct in local_accounts:
            print("=" * 100)
            print(f"Account: {acct.name!r}  (local id {acct.id}, akahu id {acct.akahu_id})")
            print("=" * 100)

            try:
                raw_txs = await fetch_account_transactions(acct.akahu_id, start_utc, end_utc)
            except (AkahuConfigError, AkahuAPIError) as exc:
                print(f"  Akahu fetch failed: {exc}\n")
                continue

            print(f"  Akahu returned {len(raw_txs)} transaction(s) in this window.")
            if raw_txs:
                nz_dates = [utc_iso_to_nz_date(r.get("date"), None) for r in raw_txs]
                nz_dates = [d for d in nz_dates if d]
                if nz_dates:
                    print(f"  Date span (NZ): {min(nz_dates).isoformat()} -> {max(nz_dates).isoformat()}")

            # Which of these are already stored locally?
            ids = [r.get("_id") for r in raw_txs if r.get("_id")]
            existing_ids: set[str] = set()
            if ids:
                existing_ids = set(
                    (await db.execute(
                        select(Transaction.akahu_transaction_id).where(
                            Transaction.source == AKAHU_SOURCE,
                            Transaction.akahu_transaction_id.in_(ids),
                        )
                    )).scalars().all()
                )

            print()
            print(f"  {'NZ date':<11s} {'raw UTC date':<26s} {'amount':>11s}  "
                  f"{'in DB':<6s} {'reference':<16s} description")
            print(f"  {'-'*11} {'-'*26} {'-'*11}  {'-'*6} {'-'*16} {'-'*40}")

            for r in sorted(raw_txs, key=lambda x: x.get("date", "")):
                nz_d = utc_iso_to_nz_date(r.get("date"), None)
                meta = r.get("meta") or {}
                in_db = "yes" if r.get("_id") in existing_ids else "NO"
                print(f"  {(nz_d.isoformat() if nz_d else '?'):<11s} "
                      f"{str(r.get('date', '?')):<26s} "
                      f"{float(r.get('amount', 0)):>11,.2f}  "
                      f"{in_db:<6s} "
                      f"{str(meta.get('reference', ''))[:16]:<16s} "
                      f"{str(r.get('description', ''))[:50]}")

            new_count = sum(1 for i in ids if i not in existing_ids)
            print()
            print(f"  Summary: {len(raw_txs)} fetched, "
                  f"{len(existing_ids)} already in DB, {new_count} not yet imported.")
            print()

    await engine.dispose()


def main() -> None:
    args = [a for a in sys.argv[1:] if a]
    if len(args) < 2:
        print(__doc__)
        return
    try:
        start = parse_iso_date(args[0])
        end = parse_iso_date(args[1])
    except ValueError:
        print("START and END must be ISO dates (YYYY-MM-DD).")
        return
    name_filter = args[2] if len(args) >= 3 else None
    asyncio.run(run(start, end, name_filter))


if __name__ == "__main__":
    main()
