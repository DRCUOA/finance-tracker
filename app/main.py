import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.jobs.balance_sync_job import run_daily_balance_sync
from app.jobs.interest_job import run_daily_interest_accrual
from app.templating import BASE_DIR
from app.routers import (
    accounts,
    auth,
    backup,
    bank_feeds,
    budgets,
    categories,
    commitments,
    dashboard,
    help,
    imports,
    matching_rules,
    printable_statement,
    profile,
    reconciliation,
    reports,
    spending,
    transactions,
)
# sql_tool import intentionally omitted — route disabled below (see audit §1.6)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the APScheduler instance alongside the FastAPI app.

    We register two daily jobs: ``run_daily_interest_accrual`` (accrues
    interest on every eligible account) and ``run_daily_balance_sync`` (pulls
    every linked account's bank-reported balance so it can't silently drift
    stale). APScheduler is imported lazily so that tooling which imports
    ``app.main`` without running the server (e.g. for reflection) doesn't pay
    the dep cost or fail if the package isn't installed — though in practice
    it's a hard dep via requirements.txt.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler(timezone="UTC")
    # 00:15 UTC daily — offset slightly from midnight so overnight batch
    # jobs have settled before we post interest for the new day.
    scheduler.add_job(
        run_daily_interest_accrual,
        CronTrigger(hour=0, minute=15),
        id="daily_interest_accrual",
        replace_existing=True,
        misfire_grace_time=60 * 60 * 6,  # still run if the app was down <6h
    )
    # 00:30 UTC daily — after interest accrual so the reported balance we pull
    # reflects a settled state for the new day.
    scheduler.add_job(
        run_daily_balance_sync,
        CronTrigger(hour=0, minute=30),
        id="daily_balance_sync",
        replace_existing=True,
        misfire_grace_time=60 * 60 * 6,  # still run if the app was down <6h
    )
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("scheduler started with daily interest accrual + balance sync")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Finla", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard", status_code=302)


app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(dashboard.router)
app.include_router(spending.router)
app.include_router(budgets.router)
app.include_router(commitments.router)
app.include_router(accounts.router)
app.include_router(categories.router)
app.include_router(matching_rules.router)
app.include_router(transactions.router)
app.include_router(imports.router)
app.include_router(bank_feeds.router)
app.include_router(reconciliation.router)
app.include_router(reports.router)
app.include_router(printable_statement.router)
# DISABLED 2026-06-11: sql_tool route has a critical cross-tenant isolation hole
# (AND/OR precedence in _inject_user_filter lets any user read/delete all tenants'
# transactions). See docs/audit/finla-audit-2026-06.md §1.6. Re-enable only after
# security compartment #0 redesigns the query scoping.
# app.include_router(sql_tool.router)
app.include_router(backup.router)
app.include_router(help.router)
