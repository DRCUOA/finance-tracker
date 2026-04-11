from decimal import Decimal

from pydantic import BaseModel


class InlineCategoryUpdate(BaseModel):
    name: str | None = None
    budgeted_amount: Decimal | None = None
    reserve_amount: Decimal | None = None
    is_fixed: bool | None = None


class InlineCategoryCreate(BaseModel):
    name: str
    category_type: str = "expense"
    parent_id: str | None = None
    budgeted_amount: Decimal = Decimal("0.00")
    reserve_amount: Decimal = Decimal("0.00")
    is_fixed: bool = False


class KeywordSync(BaseModel):
    keywords: list[str]
