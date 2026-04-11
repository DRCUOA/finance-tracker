"""One-off back-calculation of initial_balance for Akahu-linked accounts.

Sets initial_balance = current_balance - SUM(all transaction amounts) so that
the Net Worth Trend chart (which reconstructs balances from initial_balance +
transactions) agrees with the summary cards (which use current_balance).

Dry-run by default. Pass --commit to apply changes.

Usage:
    source .venv/bin/activate
    PYTHONPATH=. python scripts/backfill_initial_balances.py            # preview
    PYTHONPATH=. python scripts/backfill_initial_balances.py --commit    # apply
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

QUERY = text("""
    SELECT
        a.id,
        a.name,
        a.account_type,
        a.current_balance,
        a.initial_balance,
        COALESCE(SUM(t.amount), 0) AS tx_sum
    FROM accounts a
    LEFT JOIN transactions t ON t.account_id = a.id
    WHERE a.akahu_id IS NOT NULL
    GROUP BY a.id
    ORDER BY a.name
""")

UPDATE = text("""
    UPDATE accounts SET initial_balance = :new_initial WHERE id = :id
""")


async def run(commit: bool = False):
    engine = create_async_engine(settings.DATABASE_URL)

    async with engine.begin() as conn:
        rows = (await conn.execute(QUERY)).fetchall()

        if not rows:
            print("No Akahu-linked accounts found.")
            await engine.dispose()
            return

        print(f"Found {len(rows)} Akahu-linked account(s)\n")
        print(f"  {'Account':<30s}  {'Type':<12s}  {'Current':>14s}  {'Tx Sum':>14s}  "
              f"{'Old Initial':>14s}  {'New Initial':>14s}  {'Delta':>14s}")
        print(f"  {'-'*30}  {'-'*12}  {'-'*14}  {'-'*14}  {'-'*14}  {'-'*14}  {'-'*14}")

        changed = 0
        skipped = 0

        for row in rows:
            acct_id = row.id
            name = row.name
            acct_type = row.account_type
            current = row.current_balance
            old_initial = row.initial_balance
            tx_sum = row.tx_sum

            new_initial = current - tx_sum
            delta = new_initial - old_initial

            flag = " *" if delta != 0 else ""
            print(f"  {name:<30s}  {acct_type:<12s}  {current:>14,.2f}  {tx_sum:>14,.2f}  "
                  f"{old_initial:>14,.2f}  {new_initial:>14,.2f}  {delta:>14,.2f}{flag}")

            if delta != 0:
                changed += 1
                if commit:
                    await conn.execute(UPDATE, {"new_initial": new_initial, "id": acct_id})
            else:
                skipped += 1

        print(f"\n  {changed} account(s) need updating, {skipped} already correct.")

        if commit and changed > 0:
            print("  Changes committed.")
        elif commit:
            print("  Nothing to commit.")
        else:
            if changed > 0:
                raise Exception("Dry run — rolling back.")

    await engine.dispose()


def main():
    commit = "--commit" in sys.argv
    try:
        asyncio.run(run(commit=commit))
    except Exception as exc:
        print(f"\n  {exc}")


if __name__ == "__main__":
    main()
