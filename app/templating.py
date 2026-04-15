from datetime import date, datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import APP_VERSION

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _nzd(value, show_sign=False) -> str:
    """Format a number as NZD with thousands separator: $1,234.56"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    prefix = "-" if n < 0 else ("+" if show_sign and n > 0 else "")
    formatted = f"{abs(n):,.2f}"
    return f"{prefix}${formatted}"


def _timeago(value) -> str:
    """Human-readable relative time: '3 hours ago', '2 days ago', 'just now'."""
    if value is None:
        return "never"
    now = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        diff = now - value
    else:
        return str(value)

    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    if months == 1:
        return "1 month ago"
    return f"{months} months ago"


def _days_ago(value) -> int | None:
    """Number of days between a date/datetime and today (NZ time). None if input is None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return (date.today() - value).days
    return None


templates.env.filters["nzd"] = _nzd
templates.env.filters["timeago"] = _timeago
templates.env.filters["days_ago"] = _days_ago
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["today_date"] = date.today
