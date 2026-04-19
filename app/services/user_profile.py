import uuid
from pathlib import Path

from fastapi import UploadFile

from app.templating import BASE_DIR

AVATAR_MAX_BYTES = 2 * 1024 * 1024
UPLOAD_ROOT = BASE_DIR / "static" / "uploads" / "avatars"

_DEFAULT_PREFS: dict = {
    "compact_tables": False,
    "dashboard_default_period": "month",
}


def preferences_with_defaults(raw: dict | None) -> dict:
    merged = dict(_DEFAULT_PREFS)
    if raw and isinstance(raw, dict):
        merged.update(raw)
    if merged.get("dashboard_default_period") not in ("week", "month"):
        merged["dashboard_default_period"] = "month"
    merged["compact_tables"] = bool(merged.get("compact_tables"))
    return merged


def _sniff_content_type(data: bytes) -> str | None:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


async def save_avatar_file(user_id: uuid.UUID, upload: UploadFile) -> tuple[str | None, str | None]:
    """Returns (relative_static_path, error_message). Path is relative to static/, e.g. uploads/avatars/..."""
    content = await upload.read()
    if not content:
        return None, "Choose an image file"
    if len(content) > AVATAR_MAX_BYTES:
        return None, f"Image must be at most {AVATAR_MAX_BYTES // (1024 * 1024)} MB"

    ct = _sniff_content_type(content)
    if not ct:
        ct = (upload.content_type or "").split(";")[0].strip().lower() or None
    if ct not in _EXT:
        return None, "Use JPEG, PNG, GIF, or WebP"

    ext = _EXT[ct]
    user_dir = UPLOAD_ROOT / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    fname = f"{uuid.uuid4().hex}{ext}"
    dest = user_dir / fname
    dest.write_bytes(content)

    rel = f"uploads/avatars/{user_id}/{fname}"
    return rel, None


def delete_avatar_files(avatar_filename: str | None) -> None:
    if not avatar_filename:
        return
    rel = avatar_filename.lstrip("/")
    if not rel.startswith("uploads/avatars/"):
        return
    path = (BASE_DIR / "static").joinpath(*rel.split("/"))
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
    try:
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
