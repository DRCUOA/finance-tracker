"""Scheduled interest accrual.

Runs daily via APScheduler. Opens a dedicated DB session (we're outside a
FastAPI request so ``get_db``'s generator isn't available) and delegates to
:func:`app.services.interest.accrue_due_interest`. The service does the
per-account loop and computes how much to post using the wall-clock delta
since each account's last accrual — so a missed run (host down overnight,
etc.) self-heals on the next invocation rather than skipping interest.
"""
from __future__ import annotations

import logging

from app.database import async_session
from app.services.interest import accrue_due_interest


log = logging.getLogger(__name__)


async def run_daily_interest_accrual() -> None:
    """Entry point registered with APScheduler."""
    async with async_session() as session:
        try:
            posted = await accrue_due_interest(session)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("interest accrual failed")
            return
    log.info("posted %d interest transactions", len(posted))
