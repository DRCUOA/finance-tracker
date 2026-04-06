from decimal import Decimal

from pydantic import BaseModel


class InlineCategoryUpdate(BaseModel):
    name: str | None = None
    budgeted_amount: Decimal | None = None


class InlineCategoryCreate(BaseModel):
    name: str
    category_type: str = "expense"
    parent_id: str | None = None
    budgeted_amount: Decimal = Decimal("0.00")


class KeywordSync(BaseModel):
    keywords: list[str]
