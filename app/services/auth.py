import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.login_attempt import LoginAttempt
from app.models.user import RefreshToken, User

# Lock the (email, ip) pair after this many failed attempts inside the window.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = timedelta(minutes=15)

# A refresh token is single-use: the first request that presents it after the
# access token has expired retires it and is issued a fresh pair. Requests that
# were already in flight with the *old* cookies (a page firing several fetches
# at once) would otherwise bounce to login, so a retired token keeps working —
# without minting anything further — for this short grace window.
REFRESH_REUSE_GRACE = timedelta(seconds=60)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": user_id, "exp": expires, "type": "access"},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def _hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_refresh_token(db: AsyncSession, user_id: uuid.UUID) -> str:
    raw = secrets.token_urlsafe(64)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    rt = RefreshToken(user_id=user_id, token_hash=_hash_refresh(raw), expires_at=expires)
    db.add(rt)
    await db.flush()
    return raw


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of presenting a refresh token.

    ``rotate`` is True when this presentation retired the token, i.e. the
    caller must issue (and set) a fresh access + refresh pair. It is False for
    a retired token still inside ``REFRESH_REUSE_GRACE``: the request is
    served, but the pair minted by the first presentation stands.
    """

    user: User
    rotate: bool


async def validate_refresh_token(db: AsyncSession, raw_token: str) -> RefreshOutcome | None:
    now = datetime.now(timezone.utc)
    h = _hash_refresh(raw_token)
    stmt = select(RefreshToken).where(
        RefreshToken.token_hash == h,
        RefreshToken.expires_at > now,
        # Live, or retired-by-rotation and still inside the grace window. A
        # token revoked outright (logout, password change) keeps its original
        # far-off expiry and so never satisfies the second arm.
        or_(
            RefreshToken.is_revoked.is_(False),
            RefreshToken.expires_at <= now + REFRESH_REUSE_GRACE,
        ),
    )
    result = await db.execute(stmt)
    rt = result.scalar_one_or_none()
    if not rt:
        return None
    user = await db.get(User, rt.user_id)
    if not user:
        return None
    if rt.is_revoked:
        return RefreshOutcome(user=user, rotate=False)
    # First presentation: retire it, leaving the short reuse grace for
    # requests already in flight with the old cookie.
    rt.is_revoked = True
    rt.expires_at = now + REFRESH_REUSE_GRACE
    return RefreshOutcome(user=user, rotate=True)


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Kill every token for the user outright — including any retired-by-
    rotation token still inside its reuse grace, hence the expiry collapse."""
    now = datetime.now(timezone.utc)
    stmt = select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.expires_at > now)
    result = await db.execute(stmt)
    for rt in result.scalars():
        rt.is_revoked = True
        rt.expires_at = now


async def register_user(db: AsyncSession, email: str, password: str, display_name: str) -> User | None:
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        return None
    user = User(email=email, password_hash=hash_password(password), display_name=display_name)
    db.add(user)
    await db.flush()
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None
    return await db.get(User, uid)


async def is_login_locked(db: AsyncSession, email: str, ip: str) -> bool:
    """True once the (email, ip) pair has too many recent failures.

    A successful login clears the counter (see ``reset_login_attempts``), so the
    count only ever reflects an unbroken run of failures inside the window.
    """
    since = datetime.now(timezone.utc) - LOGIN_WINDOW
    stmt = select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.email == email,
        LoginAttempt.ip == ip,
        LoginAttempt.successful.is_(False),
        LoginAttempt.created_at >= since,
    )
    result = await db.execute(stmt)
    return (result.scalar_one() or 0) >= LOGIN_MAX_ATTEMPTS


async def record_failed_login(db: AsyncSession, email: str, ip: str) -> None:
    db.add(LoginAttempt(email=email, ip=ip, successful=False))
    await db.flush()


async def reset_login_attempts(db: AsyncSession, email: str, ip: str) -> None:
    """Clear the failure counter for the pair after a successful login."""
    await db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.email == email,
            LoginAttempt.ip == ip,
        )
    )
