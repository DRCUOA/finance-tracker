"""Routes for the user-facing printable statement feature.

Two endpoints:

* ``GET /statements``      — picker form (pick accounts + date range)
* ``GET /statements/view`` — the actual statement. Rendered as a standalone
                              HTML page that auto-triggers ``window.print()``
                              on load so the user lands in the OS print
                              dialog and can confirm scaling / save-as-PDF.

We deliberately keep this as a GET so the statement URL is bookmarkable and
re-openable. Nothing here writes to the database — the packet is computed
from live balances each time the URL is hit.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import AccountGroup
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc
from app.services import printable_statement as stmt_svc
from app.templating import templates


router = APIRouter(prefix="/statements", tags=["statements"])


def _default_range() -> tuple[date, date]:
    """Default picker range — the current calendar month to today.

    This matches what a user typically wants when they open the picker: a
    month-to-date view of activity. Easy to override via the quick-range
    buttons.
    """
    today = date.today()
    return today.replace(day=1), today


def _parse_iso_date(raw: str, fallback: date) -> date:
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return fallback


def _parse_uuid_list(raw: list[str]) -> list[uuid.UUID]:
    """Best-effort parse — silently drop malformed entries.

    The picker always sends valid UUIDs via checkboxes, but hand-crafted URLs
    should fail gracefully (empty set → router redirects to picker) rather
    than 500.
    """
    out: list[uuid.UUID] = []
    for r in raw:
        try:
            out.append(uuid.UUID(r))
        except (ValueError, TypeError):
            continue
    return out


@router.get("", response_class=HTMLResponse)
async def picker(
    request: Request,
    account_id: list[str] = Query(default=[]),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the account + date-range picker."""
    all_accounts = await acct_svc.get_accounts(db, user.id, active_only=False)
    # Split Assets / Liabilities so the template can group them — matches
    # /accounts visual hierarchy so the two pages feel consistent.
    assets = [a for a in all_accounts if a.group == AccountGroup.ASSET]
    liabilities = [a for a in all_accounts if a.group == AccountGroup.LIABILITY]

    default_from, default_to = _default_range()

    return templates.TemplateResponse(
        request,
        "printable_statement/picker.html",
        {
            "user": user,
            "accounts": all_accounts,
            "assets": assets,
            "liabilities": liabilities,
            "default_from": default_from.isoformat(),
            "default_to": default_to.isoformat(),
            "preselected": set(account_id),
            "error": None,
        },
    )


@router.get("/view", response_class=HTMLResponse)
async def view_statement(
    request: Request,
    account_id: list[str] = Query(default=[]),
    # Query params named to match the picker form; `from` is a Python
    # keyword so FastAPI exposes it via alias.
    from_: str = Query(default="", alias="from"),
    to: str = Query(default=""),
    rows_per_page: int = Query(default=stmt_svc.DEFAULT_ROWS_PER_PAGE, ge=5, le=100),
    # Any truthy-ish value flips the statement into greyscale mode — the
    # template applies ``body.greyscale`` which desaturates via CSS
    # ``filter: grayscale(1)``. We accept several common truthy spellings
    # because this URL is user-visible (they might bookmark or hand-edit it)
    # and the checkbox on the picker sends "on".
    greyscale: str = Query(default=""),
    # One of "xl" / "l" / "m" / "s" / "xs". Anything else falls back to "l"
    # (the original default). Case-insensitive for bookmarked/hand-typed
    # URLs. The template applies `body.size-<value>` which scales every
    # font-size declaration proportionally while leaving physical page
    # dimensions untouched.
    font_size: str = Query(default="l"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the print-optimised statement and auto-trigger the print dialog.

    On any validation failure we bounce back to the picker with a friendly
    error message rather than raising a 422 — the URL is user-visible so a
    broken state should always recover with a nudge, not a stack trace.
    """
    acct_uuids = _parse_uuid_list(account_id)
    if not acct_uuids:
        return RedirectResponse(url="/statements", status_code=302)

    default_from, default_to = _default_range()
    start = _parse_iso_date(from_, default_from)
    end = _parse_iso_date(to, default_to)

    if end < start:
        # Swap silently — user likely picked them in reverse.
        start, end = end, start

    # Cap the range at ~10 years to stop runaway queries / memory blow-ups
    # from pathological URLs. In practice bank statements are monthly so this
    # ceiling is comfortable.
    if end - start > timedelta(days=366 * 10):
        start = end - timedelta(days=366 * 10)

    packet = await stmt_svc.build_statement(
        db,
        user,
        acct_uuids,
        start,
        end,
        rows_per_page=rows_per_page,
    )

    if not packet.accounts:
        # All supplied account IDs were invalid for this user — send them
        # back to the picker. Preserve the typed dates so they don't lose
        # their picker state.
        return RedirectResponse(
            url=f"/statements?from={start.isoformat()}&to={end.isoformat()}",
            status_code=302,
        )

    greyscale_on = greyscale.strip().lower() in {"1", "true", "yes", "on"}

    # Validate font_size; silently normalise anything unknown to the default
    # so the URL stays friendly.
    size_choice = font_size.strip().lower()
    if size_choice not in {"xl", "l", "m", "s", "xs"}:
        size_choice = "l"

    return templates.TemplateResponse(
        request,
        "printable_statement/statement.html",
        {
            "packet": packet,
            "user": user,
            "greyscale": greyscale_on,
            "font_size": size_choice,
        },
    )
