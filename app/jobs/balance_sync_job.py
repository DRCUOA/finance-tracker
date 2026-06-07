"""Scheduled bank-feed balance sync.

Runs daily via APScheduler. ``reported_balance`` otherwise only refreshes on a
transaction sync or the manual balance-sync button, so a linked account nobody
touches drifts stale and the feed-reconciliation delta ends up comparing a
stale balance against fresh transactions. This pulls every linked account's
current balance once a day so staleness self-heals even without user action.

Opens a dedicated DB session (we're outside a FastAPI request so ``get_db``'s
generator isn't available) and delegates to
:func:`app.services.akahu.sync_account_balances` per user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.account import Account
from app.services.akahu import is_configured, sync_account_balances


log = logging.getLogger(__name__)


async def run_daily_balance_sync() -> None:
    """Entry point registered with APScheduler."""
    if not is_configured():
        log.info("balance sync skipped — Akahu not configured")
        return

    total_updated = 0
    async with async_session() as session:
        try:
            user_ids = (await session.execute(
                select(Account.user_id)
                .where(Account.akahu_id.isnot(None))
                .distinct()
            )).scalars().all()

            for user_id in user_ids:
                summary = await sync_account_balances(session, user_id)
                total_updated += summary.get("updated", 0)
                if summary.get("errors"):
                    log.warning(
                        "balance sync errors for user %s: %s",
                        user_id, summary["errors"],
                    )
                # Stamp our own sync clock so the UI's "synced Xh ago" reflects
                # the automated pull, mirroring the manual /bank-feeds/sync flow.
                if summary.get("updated"):
                    now = datetime.now(timezone.utc)
                    linked = (await session.execute(
                        select(Account).where(
                            Account.user_id == user_id,
                            Account.akahu_id.isnot(None),
                        )
                    )).scalars().all()
                    for acct in linked:
                        acct.last_synced_at = now

            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("daily balance sync failed")
            return

    log.info("daily balance sync updated %d account balances", total_updated)
