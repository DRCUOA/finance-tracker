import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services.akahu import (
    AKAHU_SOURCE,
    AkahuAPIError,
    AkahuConfigError,
    fetch_accounts as akahu_fetch_accounts,
    is_configured as akahu_is_configured,
    nz_date_to_utc_range,
    sync_account_balances,
    sync_account_transactions,
)
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/bank-feeds", tags=["bank_feeds"])


def _toast_redirect(url: str, message: str, toast_type: str = "success") -> RedirectResponse:
    """Redirect with a toast message via HX-Trigger header."""
    resp = RedirectResponse(url=url, status_code=302)
    import json
    resp.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": message, "type": toast_type}}
    )
    return resp


@router.get("", response_class=HTMLResponse)
async def bank_feeds_page(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    configured = akahu_is_configured()
    akahu_accounts: list[dict] = []
    akahu_error: str | None = None

    if configured:
        try:
            akahu_accounts = await akahu_fetch_accounts()
        except AkahuConfigError as exc:
            akahu_error = str(exc)
        except AkahuAPIError as exc:
            log.warning("Akahu API error on bank-feeds page: %s", exc)
            akahu_error = f"Could not reach Akahu: {exc}"
        except Exception as exc:
            log.exception("Unexpected error fetching Akahu accounts")
            akahu_error = "Unexpected error connecting to Akahu"

    local_accounts = await acct_svc.get_accounts(db, user.id, active_only=False)

    linked_akahu_ids = {a.akahu_id for a in local_accounts if a.akahu_id}
    unlinked_local = [
        a for a in local_accounts if not a.akahu_id and a.is_active
    ]

    link_map: dict[str, Account] = {}
    for acct in local_accounts:
        if acct.akahu_id:
            link_map[acct.akahu_id] = acct

    # Query latest transaction date and count for each linked account
    linked_ids = [a.id for a in local_accounts if a.akahu_id]
    feed_stats: dict[uuid.UUID, dict] = {}
    if linked_ids:
        stmt = (
            select(
                Transaction.account_id,
                sa_func.max(Transaction.date).label("latest_date"),
                sa_func.count(Transaction.id).label("tx_count"),
            )
            .where(
                Transaction.account_id.in_(linked_ids),
                Transaction.source == AKAHU_SOURCE,
            )
            .group_by(Transaction.account_id)
        )
        rows = await db.execute(stmt)
        for row in rows:
            feed_stats[row.account_id] = {
                "latest_date": row.latest_date,
                "tx_count": row.tx_count,
            }

    return templates.TemplateResponse(request, "bank_feeds/index.html", {
        "user": user,
        "configured": configured,
        "akahu_error": akahu_error,
        "akahu_accounts": akahu_accounts,
        "link_map": link_map,
        "unlinked_local": unlinked_local,
        "feed_stats": feed_stats,
    })


@router.post("/link")
async def link_account(
    request: Request,
    akahu_id: str = Form(...),
    account_id: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        return _toast_redirect("/bank-feeds", "Invalid account ID", "error")

    acct = await acct_svc.get_account(db, acct_uuid, user.id)
    if not acct:
        return _toast_redirect("/bank-feeds", "Account not found", "error")

    existing = await db.execute(
        select(Account).where(Account.akahu_id == akahu_id)
    )
    if existing.scalar_one_or_none():
        return _toast_redirect(
            "/bank-feeds",
            "That Akahu account is already linked to another local account",
            "error",
        )

    try:
        akahu_accounts = await akahu_fetch_accounts()
    except (AkahuConfigError, AkahuAPIError) as exc:
        return _toast_redirect("/bank-feeds", f"Akahu error: {exc}", "error")

    if not any(a["_id"] == akahu_id for a in akahu_accounts):
        return _toast_redirect("/bank-feeds", "Akahu account not found", "error")

    acct.akahu_id = akahu_id
    await db.flush()
    return _toast_redirect("/bank-feeds", f"Linked {acct.name}")


@router.post("/unlink/{account_id}")
async def unlink_account(
    account_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    acct = await acct_svc.get_account(db, account_id, user.id)
    if not acct:
        return _toast_redirect("/bank-feeds", "Account not found", "error")

    name = acct.name
    acct.akahu_id = None
    await db.flush()
    return _toast_redirect("/bank-feeds", f"Unlinked {name}")


@router.post("/sync")
async def sync_balances(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        summary = await sync_account_balances(db, user.id)
    except (AkahuConfigError, AkahuAPIError) as exc:
        return _toast_redirect("/bank-feeds", f"Sync failed: {exc}", "error")
    except Exception:
        log.exception("Unexpected error during balance sync")
        return _toast_redirect("/bank-feeds", "Unexpected sync error", "error")

    if summary.get("updated"):
        now = datetime.now(timezone.utc)
        stmt = select(Account).where(
            Account.user_id == user.id,
            Account.akahu_id.isnot(None),
        )
        rows = await db.execute(stmt)
        for acct in rows.scalars():
            acct.last_synced_at = now
        await db.flush()

    parts = []
    if summary["updated"]:
        parts.append(f"{summary['updated']} updated")
    if summary["unchanged"]:
        parts.append(f"{summary['unchanged']} unchanged")
    if summary["missing_in_akahu"]:
        parts.append(f"{summary['missing_in_akahu']} missing in Akahu")
    if summary["errors"]:
        parts.append(f"{len(summary['errors'])} errors")
        toast_type = "warning"
    else:
        toast_type = "success"

    msg = "Balance sync: " + (", ".join(parts) if parts else "nothing to sync")
    return _toast_redirect("/bank-feeds", msg, toast_type)


@router.post("/sync-transactions/{account_id}")
async def sync_transactions(
    account_id: uuid.UUID,
    start_date: str = Form(...),
    end_date: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
    except ValueError:
        return _toast_redirect("/bank-feeds", "Invalid date format", "error")

    if sd > ed:
        return _toast_redirect("/bank-feeds", "Start date must be before end date", "error")

    start_utc, end_utc = nz_date_to_utc_range(sd, ed)

    try:
        summary = await sync_account_transactions(db, user.id, account_id, start_utc, end_utc)
    except (AkahuConfigError, AkahuAPIError) as exc:
        return _toast_redirect("/bank-feeds", f"Transaction sync failed: {exc}", "error")
    except Exception:
        log.exception("Unexpected error during transaction sync")
        return _toast_redirect("/bank-feeds", "Unexpected transaction sync error", "error")

    if summary["errors"]:
        msg = "Sync error: " + "; ".join(summary["errors"])
        return _toast_redirect("/bank-feeds", msg, "error")

    acct = await db.get(Account, account_id)
    if acct:
        acct.last_synced_at = datetime.now(timezone.utc)
        await db.flush()

    parts = []
    if summary["inserted"]:
        parts.append(f"{summary['inserted']} new")
    if summary["updated"]:
        parts.append(f"{summary['updated']} updated")
    if summary["unchanged"]:
        parts.append(f"{summary['unchanged']} unchanged")
    if summary["stale_marked"]:
        parts.append(f"{summary['stale_marked']} stale")
    if summary["stale_cleared"]:
        parts.append(f"{summary['stale_cleared']} restored")

    msg = f"Synced {summary['fetched']} transactions: " + (
        ", ".join(parts) if parts else "no changes"
    )
    return _toast_redirect("/bank-feeds", msg)
