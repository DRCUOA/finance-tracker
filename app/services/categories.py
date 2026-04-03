import uuid
from decimal import Decimal

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category, CategoryKeyword, CategoryType


DEFAULT_CATEGORIES = [
    ("Income", CategoryType.INCOME, [
        ("Salary", []),
        ("Freelance", []),
        ("Interest", []),
        ("Other Income", []),
    ]),
    ("Housing", CategoryType.EXPENSE, [
        ("Rent/Mortgage", ["rent", "mortgage"]),
        ("Utilities", ["electric", "gas", "water", "utility"]),
        ("Insurance", ["home insurance", "renters insurance"]),
        ("Maintenance", []),
    ]),
    ("Transport", CategoryType.EXPENSE, [
        ("Fuel", ["fuel", "petrol", "gas station", "shell", "bp"]),
        ("Public Transit", ["metro", "bus", "train", "uber", "lyft"]),
        ("Car Payment", ["car loan", "auto loan"]),
        ("Parking", ["parking"]),
    ]),
    ("Food", CategoryType.EXPENSE, [
        ("Groceries", ["grocery", "supermarket", "walmart", "costco", "aldi", "kroger"]),
        ("Restaurants", ["restaurant", "cafe", "mcdonald", "starbucks", "pizza"]),
        ("Delivery", ["doordash", "ubereats", "grubhub"]),
    ]),
    ("Personal", CategoryType.EXPENSE, [
        ("Healthcare", ["pharmacy", "doctor", "hospital", "medical"]),
        ("Clothing", ["clothing", "apparel", "shoes"]),
        ("Education", ["tuition", "books", "course"]),
        ("Entertainment", ["netflix", "spotify", "cinema", "theater"]),
        ("Subscriptions", ["subscription"]),
    ]),
    ("Financial", CategoryType.EXPENSE, [
        ("Bank Fees", ["bank fee", "overdraft", "atm fee"]),
        ("Loan Payment", ["loan payment"]),
        ("Credit Card Payment", []),
    ]),
    ("Transfers", CategoryType.TRANSFER, []),
]


async def seed_default_categories(db: AsyncSession, user_id: uuid.UUID) -> None:
    order = 0
    for parent_name, cat_type, children in DEFAULT_CATEGORIES:
        parent = Category(
            user_id=user_id, name=parent_name, category_type=cat_type,
            sort_order=order, parent_id=None,
        )
        db.add(parent)
        await db.flush()
        order += 1

        if isinstance(children, list):
            child_order = 0
            for child_info in children:
                if isinstance(child_info, tuple):
                    child_name, keywords = child_info
                else:
                    child_name = child_info
                    keywords = []
                child = Category(
                    user_id=user_id, name=child_name, category_type=cat_type,
                    sort_order=child_order, parent_id=parent.id,
                )
                db.add(child)
                await db.flush()
                for kw in keywords:
                    db.add(CategoryKeyword(category_id=child.id, keyword=kw.lower()))
                child_order += 1


async def get_category_tree(db: AsyncSession, user_id: uuid.UUID) -> list[Category]:
    stmt = (
        select(Category)
        .where(Category.user_id == user_id, Category.parent_id.is_(None))
        .options(selectinload(Category.children).selectinload(Category.keywords))
        .options(selectinload(Category.keywords))
        .order_by(Category.sort_order)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_all_categories_flat(db: AsyncSession, user_id: uuid.UUID) -> list[Category]:
    stmt = (
        select(Category)
        .where(Category.user_id == user_id)
        .options(selectinload(Category.children))
        .options(selectinload(Category.keywords))
        .order_by(Category.sort_order)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_category(
    db: AsyncSession, user_id: uuid.UUID, name: str,
    category_type: CategoryType, parent_id: uuid.UUID | None = None,
    budgeted_amount: Decimal = Decimal("0.00"),
) -> Category:
    max_order = await db.execute(
        select(sa_func.coalesce(sa_func.max(Category.sort_order), -1))
        .where(Category.user_id == user_id, Category.parent_id == parent_id)
    )
    next_order = max_order.scalar() + 1

    cat = Category(
        user_id=user_id, name=name, category_type=category_type,
        parent_id=parent_id, sort_order=next_order, budgeted_amount=budgeted_amount,
    )
    db.add(cat)
    await db.flush()
    return cat


async def update_category(
    db: AsyncSession, category_id: uuid.UUID, user_id: uuid.UUID, **kwargs
) -> Category | None:
    cat = await db.get(Category, category_id)
    if not cat or cat.user_id != user_id:
        return None
    for k, v in kwargs.items():
        if hasattr(cat, k):
            setattr(cat, k, v)
    await db.flush()
    return cat


async def delete_category(db: AsyncSession, category_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    cat = await db.get(Category, category_id)
    if not cat or cat.user_id != user_id:
        return False
    await db.delete(cat)
    await db.flush()
    return True


async def add_keyword(db: AsyncSession, category_id: uuid.UUID, user_id: uuid.UUID, keyword: str) -> CategoryKeyword | None:
    cat = await db.get(Category, category_id)
    if not cat or cat.user_id != user_id:
        return None
    kw = CategoryKeyword(category_id=category_id, keyword=keyword.lower().strip())
    db.add(kw)
    await db.flush()
    return kw


async def delete_keyword(db: AsyncSession, keyword_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    kw = await db.get(CategoryKeyword, keyword_id)
    if not kw:
        return False
    cat = await db.get(Category, kw.category_id)
    if not cat or cat.user_id != user_id:
        return False
    await db.delete(kw)
    await db.flush()
    return True
