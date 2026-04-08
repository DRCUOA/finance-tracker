import uuid
from decimal import Decimal

from sqlalchemy import select, update, delete, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.budget import Budget
from app.models.category import Category, CategoryKeyword, CategoryType
from app.models.transaction import Transaction


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


async def get_category(db: AsyncSession, category_id: uuid.UUID, user_id: uuid.UUID) -> Category | None:
    cat = await db.get(Category, category_id, options=[selectinload(Category.keywords)])
    if not cat or cat.user_id != user_id:
        return None
    return cat


async def create_category(
    db: AsyncSession, user_id: uuid.UUID, name: str,
    category_type: CategoryType, parent_id: uuid.UUID | None = None,
    budgeted_amount: Decimal = Decimal("0.00"),
    is_fixed: bool = False,
) -> Category:
    max_order = await db.execute(
        select(sa_func.coalesce(sa_func.max(Category.sort_order), -1))
        .where(Category.user_id == user_id, Category.parent_id == parent_id)
    )
    next_order = max_order.scalar() + 1

    cat = Category(
        user_id=user_id, name=name, category_type=category_type,
        parent_id=parent_id, sort_order=next_order, budgeted_amount=budgeted_amount,
        is_fixed=is_fixed,
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


async def update_keyword(
    db: AsyncSession, keyword_id: uuid.UUID, user_id: uuid.UUID, new_keyword: str,
) -> CategoryKeyword | None:
    """Change the text of a keyword; normalises to lowercase. Returns None if not found or duplicate in category."""
    text = new_keyword.lower().strip()
    if not text:
        return None
    kw = await db.get(CategoryKeyword, keyword_id)
    if not kw:
        return None
    cat = await db.get(Category, kw.category_id)
    if not cat or cat.user_id != user_id:
        return None
    dup = await db.execute(
        select(CategoryKeyword.id).where(
            CategoryKeyword.category_id == kw.category_id,
            CategoryKeyword.keyword == text,
            CategoryKeyword.id != keyword_id,
        ).limit(1)
    )
    if dup.scalar():
        return None
    kw.keyword = text
    await db.flush()
    return kw


async def sync_keywords(
    db: AsyncSession, category_id: uuid.UUID, user_id: uuid.UUID, keywords: list[str],
) -> bool:
    """Replace the full keyword set for a category, preserving hit_count on survivors."""
    cat = await db.get(Category, category_id, options=[selectinload(Category.keywords)])
    if not cat or cat.user_id != user_id:
        return False

    desired = {k.lower().strip() for k in keywords if k.strip()}
    existing = {kw.keyword: kw for kw in cat.keywords}

    for word, kw_obj in existing.items():
        if word not in desired:
            await db.delete(kw_obj)

    for word in desired:
        if word not in existing:
            db.add(CategoryKeyword(category_id=category_id, keyword=word))

    await db.flush()
    return True


async def merge_categories(
    db: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID, user_id: uuid.UUID,
) -> dict | None:
    """Merge source category into target: move transactions, budgets, keywords, then delete source."""
    source = await db.get(Category, source_id, options=[selectinload(Category.keywords)])
    target = await db.get(Category, target_id, options=[selectinload(Category.keywords)])
    if not source or source.user_id != user_id:
        return None
    if not target or target.user_id != user_id:
        return None
    if source_id == target_id:
        return None

    # Move transactions
    tx_result = await db.execute(
        update(Transaction)
        .where(Transaction.category_id == source_id, Transaction.user_id == user_id)
        .values(category_id=target_id)
    )
    tx_count = tx_result.rowcount

    # Merge budgets: sum amounts for overlapping periods, move non-overlapping
    target_kw_set = {kw.keyword for kw in target.keywords}
    source_budgets = (await db.execute(
        select(Budget).where(Budget.category_id == source_id, Budget.user_id == user_id)
    )).scalars().all()

    for sb in source_budgets:
        existing = (await db.execute(
            select(Budget).where(
                Budget.category_id == target_id,
                Budget.user_id == user_id,
                Budget.year == sb.year,
                Budget.month == sb.month,
            )
        )).scalar_one_or_none()
        if existing:
            existing.amount += sb.amount
            await db.delete(sb)
        else:
            sb.category_id = target_id
    await db.flush()

    # Move keywords (skip duplicates)
    for kw in list(source.keywords):
        if kw.keyword not in target_kw_set:
            kw.category_id = target_id
            target_kw_set.add(kw.keyword)
        else:
            await db.delete(kw)
    await db.flush()

    # Delete source category
    await db.delete(source)
    await db.flush()

    return {"transactions_moved": tx_count, "source_name": source.name, "target_name": target.name}
