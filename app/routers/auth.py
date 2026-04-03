from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.templating import templates
from app.services import auth as auth_service
from app.services.categories import seed_default_categories

router = APIRouter(tags=["auth"])


class _RedirectToLogin(HTTPException):
    def __init__(self):
        super().__init__(status_code=302, headers={"Location": "/login"})


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    user_id = auth_service.decode_access_token(token)
    if not user_id:
        refresh = request.cookies.get("refresh_token")
        if not refresh:
            return None
        user = await auth_service.validate_refresh_token(db, refresh)
        if not user:
            return None
        return user
    return await auth_service.get_user_by_id(db, user_id)


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await get_current_user(request, db)
    if not user:
        raise _RedirectToLogin()
    return user


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    response.set_cookie("access_token", access_token, httponly=True, samesite="lax", max_age=3600)
    response.set_cookie("refresh_token", refresh_token, httponly=True, samesite="lax", max_age=7 * 86400)


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.authenticate_user(db, email, password)
    if not user:
        return templates.TemplateResponse(request, "auth/login.html", {"error": "Invalid email or password"}, status_code=401)

    access = auth_service.create_access_token(str(user.id))
    refresh = await auth_service.create_refresh_token(db, user.id)
    response = RedirectResponse(url="/dashboard", status_code=302)
    _set_auth_cookies(response, access, refresh)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "auth/register.html", {"error": None})


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.register_user(db, email, password, display_name)
    if not user:
        return templates.TemplateResponse(request, "auth/register.html", {"error": "Email already registered"}, status_code=400)

    await seed_default_categories(db, user.id)

    access = auth_service.create_access_token(str(user.id))
    refresh = await auth_service.create_refresh_token(db, user.id)
    response = RedirectResponse(url="/dashboard", status_code=302)
    _set_auth_cookies(response, access, refresh)
    return response


@router.get("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if user:
        await auth_service.revoke_all_refresh_tokens(db, user.id)
    response = RedirectResponse(url="/login", status_code=302)
    _clear_auth_cookies(response)
    return response
