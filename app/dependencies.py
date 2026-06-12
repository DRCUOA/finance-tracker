import uuid

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import Account
from app.models.user import User
from app.routers.auth import require_user
from app.services import accounts as acct_svc


async def get_owned_account_or_404(
    db: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID,
) -> Account:
    """Return the account iff it belongs to user_id, else 404.

    404 (not 403) so we don't leak the existence of other tenants' accounts.
    Use this for form-sourced account ids; see require_owned_account for the
    path-param dependency variant.
    """
    acct = await acct_svc.get_account(db, account_id, user_id)
    if acct is None:
        raise HTTPException(status_code=404)
    return acct


async def require_owned_account(
    account_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Path-param dependency: 404 unless {account_id} belongs to the caller."""
    return await get_owned_account_or_404(db, account_id, user.id)
