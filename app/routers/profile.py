from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.auth import _set_auth_cookies, require_user
from app.services import auth as auth_service
from app.services import user_profile as profile_svc
from app.templating import templates

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: User = Depends(require_user),
):
    prefs = profile_svc.preferences_with_defaults(user.preferences)
    return templates.TemplateResponse(
        request,
        "profile/index.html",
        {
            "user": user,
            "prefs": prefs,
            "settings_error": None,
            "settings_ok": False,
            "avatar_error": None,
            "avatar_ok": False,
            "password_error": None,
            "password_ok": False,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def update_settings(
    request: Request,
    display_name: str = Form(...),
    dashboard_default_period: str = Form("month"),
    compact_tables: str | None = Form(None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    name = (display_name or "").strip()
    if not name or len(name) > 100:
        prefs = profile_svc.preferences_with_defaults(user.preferences)
        return templates.TemplateResponse(
            request,
            "profile/index.html",
            {
                "user": user,
                "prefs": prefs,
                "settings_error": "Display name must be 1–100 characters.",
                "settings_ok": False,
                "avatar_error": None,
                "avatar_ok": False,
                "password_error": None,
                "password_ok": False,
            },
            status_code=400,
        )

    period = dashboard_default_period if dashboard_default_period in ("week", "month") else "month"
    new_prefs = dict(user.preferences or {})
    new_prefs["dashboard_default_period"] = period
    new_prefs["compact_tables"] = compact_tables in ("on", "true", "1", "yes")

    user.display_name = name
    user.preferences = new_prefs

    prefs = profile_svc.preferences_with_defaults(user.preferences)
    return templates.TemplateResponse(
        request,
        "profile/index.html",
        {
            "user": user,
            "prefs": prefs,
            "settings_error": None,
            "settings_ok": True,
            "avatar_error": None,
            "avatar_ok": False,
            "password_error": None,
            "password_ok": False,
        },
    )


@router.post("/avatar", response_class=HTMLResponse)
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    rel, err = await profile_svc.save_avatar_file(user.id, file)
    if err:
        prefs = profile_svc.preferences_with_defaults(user.preferences)
        return templates.TemplateResponse(
            request,
            "profile/index.html",
            {
                "user": user,
                "prefs": prefs,
                "settings_error": None,
                "settings_ok": False,
                "avatar_error": err,
                "avatar_ok": False,
                "password_error": None,
                "password_ok": False,
            },
            status_code=400,
        )

    old = user.avatar_filename
    user.avatar_filename = rel
    profile_svc.delete_avatar_files(old)

    prefs = profile_svc.preferences_with_defaults(user.preferences)
    return templates.TemplateResponse(
        request,
        "profile/index.html",
        {
            "user": user,
            "prefs": prefs,
            "settings_error": None,
            "settings_ok": False,
            "avatar_error": None,
            "avatar_ok": True,
            "password_error": None,
            "password_ok": False,
        },
    )


@router.post("/avatar/remove", response_class=HTMLResponse)
async def remove_avatar(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    old = user.avatar_filename
    user.avatar_filename = None
    profile_svc.delete_avatar_files(old)

    prefs = profile_svc.preferences_with_defaults(user.preferences)
    return templates.TemplateResponse(
        request,
        "profile/index.html",
        {
            "user": user,
            "prefs": prefs,
            "settings_error": None,
            "settings_ok": False,
            "avatar_error": None,
            "avatar_ok": True,
            "password_error": None,
            "password_ok": False,
        },
    )


@router.post("/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = profile_svc.preferences_with_defaults(user.preferences)

    base_ctx = {
        "user": user,
        "prefs": prefs,
        "settings_error": None,
        "settings_ok": False,
        "avatar_error": None,
        "avatar_ok": False,
        "password_error": None,
        "password_ok": False,
    }

    if not auth_service.verify_password(current_password, user.password_hash):
        base_ctx["password_error"] = "Current password is incorrect."
        return templates.TemplateResponse(request, "profile/index.html", base_ctx, status_code=400)

    if len(new_password) < 8:
        base_ctx["password_error"] = "New password must be at least 8 characters."
        return templates.TemplateResponse(request, "profile/index.html", base_ctx, status_code=400)

    if new_password != confirm_password:
        base_ctx["password_error"] = "New password and confirmation do not match."
        return templates.TemplateResponse(request, "profile/index.html", base_ctx, status_code=400)

    user.password_hash = auth_service.hash_password(new_password)
    await auth_service.revoke_all_refresh_tokens(db, user.id)

    access = auth_service.create_access_token(str(user.id))
    refresh = await auth_service.create_refresh_token(db, user.id)
    base_ctx["password_ok"] = True
    response = templates.TemplateResponse(request, "profile/index.html", base_ctx)
    _set_auth_cookies(response, access, refresh)
    return response
