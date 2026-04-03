from pathlib import Path
from fastapi.templating import Jinja2Templates

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


templates.env.filters["nzd"] = _nzd
