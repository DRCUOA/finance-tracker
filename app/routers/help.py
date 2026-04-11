from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.models.user import User
from app.routers.auth import require_user
from app.templating import templates

router = APIRouter(prefix="/help", tags=["help"])


@router.get("", response_class=HTMLResponse)
async def help_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "help/index.html", {"user": user})
