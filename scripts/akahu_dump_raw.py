"""Dump the full raw JSON Akahu returns for a few transactions.

Read-only. Used to confirm exactly which date-bearing fields Akahu provides on
a transaction object (is there a posting/settlement date distinct from ``date``,
or anything useful hidden in ``meta``?). Prints the complete, unmodified JSON of
the first few rows in the window for each linked account, optionally filtered by
description substring.

Usage:
    source .venv/bin/activate
    PYTHONPATH=. python scripts/akahu_dump_raw.py START END [ACCOUNT_NAME_SUBSTR] [DESC_SUBSTR]

Example (inspect the -973.00 loan payments):
    PYTHONPATH=. python scripts/akahu_dump_raw.py 2026-02-15 2026-04-14 "Home Loan" "88647727"
"""

import asyncio
import json
import sys

from app.models import (  # noqa: F401  (populate mapper registry)
    account,
    budget,
    category,
    commitment,
    reconciliation,
    statement,
    transaction,
    user,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.dates import parse_iso_date
from app.models.account import Account
from app.services.akahu import (
    AkahuAPIError,
    AkahuConfigError,
    fetch_account_transactions,
    is_configured,
    nz_date_to_utc_range,
)


async def run(start, end, name_filter, desc_filter, limit=4):
    if not is_configured():
        print("Akahu credentials not configured.")
        return

    start_utc, end_utc = nz_date_to_utc_range(start, end)
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        stmt = select(Account).where(Account.akahu_id.isnot(None))
        if name_filter:
            stmt = stmt.where(Account.name.ilike(f"%{name_filter}%"))
        accounts = list((await db.execute(stmt)).scalars().all())

        for acct in accounts:
            print("=" * 100)
            print(f"{acct.name!r}  (akahu id {acct.akahu_id})")
            print("=" * 100)
            try:
                raw_txs = await fetch_account_transactions(acct.akahu_id, start_utc, end_utc)
            except (AkahuConfigError, AkahuAPIError) as exc:
                print(f"  fetch failed: {exc}")
                continue

            if desc_filter:
                raw_txs = [
                    r for r in raw_txs
                    if desc_filter.lower() in json.dumps(r).lower()
                ]

            print(f"  Showing up to {limit} of {len(raw_txs)} matching row(s):\n")
            for r in raw_txs[:limit]:
                print(json.dumps(r, indent=2, default=str))
                print("-" * 80)

    await engine.dispose()


def main():
    args = [a for a in sys.argv[1:] if a]
    if len(args) < 2:
        print(__doc__)
        return
    start = parse_iso_date(args[0])
    end = parse_iso_date(args[1])
    name_filter = args[2] if len(args) >= 3 else None
    desc_filter = args[3] if len(args) >= 4 else None
    asyncio.run(run(start, end, name_filter, desc_filter))


if __name__ == "__main__":
    main()
