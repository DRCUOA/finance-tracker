"""Import data from an external finance-tracker JSON export.

The source schema ("simples-flatting-edition") stores categories in a 3-level
hierarchy where the root nodes (Expense, Income, Transfer, …) encode the
category *type* rather than being real categories.  This module flattens that
into the local 2-level model (parent → child) and maps account types, terms,
and signed amounts to match the local schema.
"""

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select, func as sa_func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountTerm, AccountType
from app.models.category import Category, CategoryKeyword, CategoryType
from app.models.statement import FileType, Statement, StatementStatus
from app.models.transaction import Transaction


# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

ACCOUNT_TYPE_MAP: dict[str, AccountType] = {
    "checking": AccountType.CHECKING,
    "savings": AccountType.SAVINGS,
    "credit": AccountType.CREDIT_CARD,
    "mortgage": AccountType.LOAN,
    "investment": AccountType.INVESTMENT,
    "cash": AccountType.CASH,
    "other": AccountType.OTHER,
}

TERM_MAP: dict[str, AccountTerm] = {
    "short": AccountTerm.SHORT,
    "mid": AccountTerm.MEDIUM,
    "medium": AccountTerm.MEDIUM,
    "long": AccountTerm.LONG,
}

ROOT_CATEGORY_TYPE: dict[str, CategoryType] = {
    "income": CategoryType.INCOME,
    "expense": CategoryType.EXPENSE,
    "transfer": CategoryType.TRANSFER,
}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class MigrationResult:
    accounts_created: int = 0
    accounts_matched: int = 0
    categories_created: int = 0
    categories_matched: int = 0
    transactions_imported: int = 0
    transactions_skipped: int = 0
    statements_created: int = 0
    skipped_descriptions: list[str] = field(default_factory=list)
    cat_id_map: dict[str, uuid.UUID] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Preview / analysis
# ---------------------------------------------------------------------------

def preview_external_data(data: dict, skip_roots: set[str] | None = None) -> dict:
    """Analyse an export and return a preview without touching the database.

    Returns a dict with account summaries so the user can pick which to import.
    """
    skip_roots = skip_roots or set()
    ext_cats = {c["category_id"]: c for c in data.get("data", {}).get("categories", [])}

    root_ids_to_skip: set[str] = set()
    for c in ext_cats.values():
        if c["parent_category_id"] is None and c["category_name"] in skip_roots:
            root_ids_to_skip.add(c["category_id"])

    def _ancestor_root(cat_id: str | None) -> str | None:
        if cat_id is None:
            return None
        c = ext_cats.get(cat_id)
        if not c:
            return None
        if c["parent_category_id"] is None:
            return c["category_id"]
        return _ancestor_root(c["parent_category_id"])

    ext_accounts = data.get("data", {}).get("accounts", [])
    ext_txns = data.get("data", {}).get("transactions", [])
    ext_stmts = data.get("data", {}).get("statementImports", [])

    acct_summaries = []
    for acct in ext_accounts:
        aid = acct["account_id"]
        txns = [t for t in ext_txns if t["account_id"] == aid]

        uncategorized = []
        categorized_eligible = []
        for t in txns:
            cid = t.get("category_id")
            if cid is None:
                uncategorized.append(t)
                continue
            root = _ancestor_root(cid)
            if root in root_ids_to_skip:
                continue
            categorized_eligible.append(t)

        stmts = [s for s in ext_stmts if s["account_id"] == aid]

        all_eligible = categorized_eligible + uncategorized
        dates = [t["transaction_date"] for t in all_eligible]
        acct_summaries.append({
            "account_id": aid,
            "name": acct["account_name"],
            "account_type": acct["account_type"],
            "account_class": acct["account_class"],
            "timeframe": acct.get("timeframe", "short"),
            "opening_balance": acct.get("opening_balance", 0),
            "current_balance": acct.get("current_balance", 0),
            "total_transactions": len(txns),
            "eligible_transactions": len(categorized_eligible),
            "uncategorized": len(uncategorized),
            "statement_imports": len(stmts),
            "date_from": min(dates) if dates else None,
            "date_to": max(dates) if dates else None,
        })

    return {"accounts": acct_summaries}


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------

async def import_external_data(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: dict,
    account_ids: set[str],
    skip_category_roots: set[str] | None = None,
    include_uncategorized_for: set[str] | None = None,
) -> MigrationResult:
    """Import selected accounts from an external JSON export.

    Parameters
    ----------
    account_ids
        Set of external account UUIDs to import (everything else is skipped).
    skip_category_roots
        Root category names to skip entirely (e.g. {"Home Loan", "House Value Adjustments"}).
    include_uncategorized_for
        Set of external account UUIDs where uncategorized transactions should
        be imported.  For all other accounts they are skipped.
    """
    skip_category_roots = skip_category_roots or set()
    include_uncategorized_for = include_uncategorized_for or set()
    result = MigrationResult()
    ext = data.get("data", {})

    ext_cats = {c["category_id"]: c for c in ext.get("categories", [])}
    ext_accounts = {a["account_id"]: a for a in ext.get("accounts", [])}
    ext_txns = ext.get("transactions", [])
    ext_stmts = ext.get("statementImports", [])

    # ---- Build root / type map ----
    root_ids_to_skip: set[str] = set()
    root_type: dict[str, CategoryType] = {}
    for c in ext_cats.values():
        if c["parent_category_id"] is None:
            name_lower = c["category_name"].lower()
            if c["category_name"] in skip_category_roots:
                root_ids_to_skip.add(c["category_id"])
            elif name_lower in ROOT_CATEGORY_TYPE:
                root_type[c["category_id"]] = ROOT_CATEGORY_TYPE[name_lower]

    def _ancestor_root(cat_id: str | None) -> str | None:
        if cat_id is None:
            return None
        c = ext_cats.get(cat_id)
        if not c:
            return None
        if c["parent_category_id"] is None:
            return c["category_id"]
        return _ancestor_root(c["parent_category_id"])

    def _category_type_for(cat_id: str) -> CategoryType:
        root = _ancestor_root(cat_id)
        return root_type.get(root, CategoryType.EXPENSE)

    def _depth(cat_id: str) -> int:
        c = ext_cats.get(cat_id)
        if not c or c["parent_category_id"] is None:
            return 0
        return 1 + _depth(c["parent_category_id"])

    # ---- Determine which categories are needed ----
    needed_cat_ids: set[str] = set()
    for t in ext_txns:
        if t["account_id"] not in account_ids:
            continue
        cid = t.get("category_id")
        if not cid:
            continue
        root = _ancestor_root(cid)
        if root in root_ids_to_skip:
            continue
        needed_cat_ids.add(cid)

    # ---- Resolve the local category for each needed external category ----
    # Strategy: root nodes (depth 0) = category_type only.
    # depth 1 = local parent, depth 2 = local child.
    # If depth 1 is used directly, it's a parent with no sub-category.
    # If depth >= 3, flatten into depth 2 (use the depth-1 ancestor as parent).

    cat_id_map: dict[str, uuid.UUID] = {}

    # Also include parent chain categories if their children are needed
    full_needed: set[str] = set()
    for cid in needed_cat_ids:
        c = ext_cats.get(cid)
        while c:
            d = _depth(c["category_id"])
            if d >= 1:
                full_needed.add(c["category_id"])
            if c["parent_category_id"] is None:
                break
            c = ext_cats.get(c["parent_category_id"])

    # Sort by depth so parents are created before children
    sorted_needed = sorted(full_needed, key=lambda cid: _depth(cid))

    for ext_cid in sorted_needed:
        ec = ext_cats[ext_cid]
        depth = _depth(ext_cid)
        cat_type = _category_type_for(ext_cid)
        cat_name = ec["category_name"].strip()
        budgeted = Decimal(str(ec.get("budgeted_amount", 0) or 0))

        if depth == 0:
            continue

        local_parent_id: uuid.UUID | None = None
        if depth == 1:
            local_parent_id = None
        elif depth >= 2:
            # Walk up to find the depth-1 ancestor → local parent
            anc = ext_cats.get(ec["parent_category_id"])
            while anc and _depth(anc["category_id"]) > 1:
                anc = ext_cats.get(anc["parent_category_id"])
            if anc:
                local_parent_id = cat_id_map.get(anc["category_id"])

        # Check for existing category with same name, type, and parent
        match_stmt = select(Category).where(
            Category.user_id == user_id,
            Category.name == cat_name,
            Category.category_type == cat_type,
        )
        if local_parent_id:
            match_stmt = match_stmt.where(Category.parent_id == local_parent_id)
        else:
            match_stmt = match_stmt.where(Category.parent_id.is_(None))

        existing = (await db.execute(match_stmt)).scalar_one_or_none()

        if existing:
            cat_id_map[ext_cid] = existing.id
            result.categories_matched += 1
        else:
            max_order = (await db.execute(
                select(sa_func.coalesce(sa_func.max(Category.sort_order), -1))
                .where(Category.user_id == user_id, Category.parent_id == local_parent_id)
            )).scalar()
            cat = Category(
                user_id=user_id,
                name=cat_name,
                category_type=cat_type,
                parent_id=local_parent_id,
                sort_order=(max_order or 0) + 1,
                budgeted_amount=budgeted,
            )
            db.add(cat)
            await db.flush()
            cat_id_map[ext_cid] = cat.id
            result.categories_created += 1

    # ---- Accounts ----
    acct_id_map: dict[str, uuid.UUID] = {}
    for ext_aid in account_ids:
        ea = ext_accounts.get(ext_aid)
        if not ea:
            continue

        acct_type = ACCOUNT_TYPE_MAP.get(ea["account_type"], AccountType.OTHER)
        acct_term = TERM_MAP.get(ea.get("timeframe", "short"), AccountTerm.SHORT)
        opening = Decimal(str(ea.get("opening_balance", 0)))

        existing = (await db.execute(
            select(Account).where(Account.user_id == user_id, Account.name == ea["account_name"])
        )).scalar_one_or_none()

        if existing:
            acct_id_map[ext_aid] = existing.id
            result.accounts_matched += 1
        else:
            max_order = (await db.execute(
                select(sa_func.coalesce(sa_func.max(Account.sort_order), -1))
                .where(Account.user_id == user_id)
            )).scalar()
            acct = Account(
                user_id=user_id,
                name=ea["account_name"],
                account_type=acct_type,
                currency="NZD",
                initial_balance=opening,
                current_balance=opening,
                term=acct_term,
                sort_order=(max_order or 0) + 1,
            )
            db.add(acct)
            await db.flush()
            acct_id_map[ext_aid] = acct.id
            result.accounts_created += 1

    # ---- Transactions ----
    for t in ext_txns:
        if t["account_id"] not in account_ids:
            continue

        cid = t.get("category_id")
        if not cid and t["account_id"] not in include_uncategorized_for:
            result.transactions_skipped += 1
            result.skipped_descriptions.append(t.get("description", "")[:80])
            continue

        if cid:
            root = _ancestor_root(cid)
            if root in root_ids_to_skip:
                result.transactions_skipped += 1
                result.skipped_descriptions.append(t.get("description", "")[:80])
                continue

        local_acct_id = acct_id_map.get(t["account_id"])
        if not local_acct_id:
            continue

        local_cat_id = cat_id_map.get(cid) if cid else None

        tx_date = date.fromisoformat(t["transaction_date"])
        amount = Decimal(str(t.get("signed_amount", t["amount"])))
        description = t.get("description", "")

        # Deduplicate: same account + date + amount + normalised description
        dup_stmt = select(Transaction.id).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.account_id == local_acct_id,
                Transaction.date == tx_date,
                Transaction.amount == amount,
                sa_func.lower(sa_func.trim(Transaction.description))
                == description.lower().strip(),
            )
        ).limit(1)
        if (await db.execute(dup_stmt)).first():
            result.transactions_skipped += 1
            result.skipped_descriptions.append(description[:80])
            continue

        tx = Transaction(
            user_id=user_id,
            account_id=local_acct_id,
            category_id=local_cat_id,
            date=tx_date,
            amount=amount,
            description=description,
            original_description=description,
        )
        db.add(tx)
        result.transactions_imported += 1

    await db.flush()

    # ---- Statement imports ----
    for s in ext_stmts:
        if s["account_id"] not in account_ids:
            continue
        local_acct_id = acct_id_map.get(s["account_id"])
        if not local_acct_id:
            continue

        start = date.fromisoformat(s["statement_from"]) if s.get("statement_from") else None
        end = date.fromisoformat(s["statement_to"]) if s.get("statement_to") else None

        stmt = Statement(
            user_id=user_id,
            account_id=local_acct_id,
            filename=s.get("source_filename", "migration"),
            file_type=FileType.CSV,
            start_date=start,
            end_date=end,
            record_count=s.get("line_count", 0),
            status=StatementStatus.IMPORTED,
        )
        db.add(stmt)
        result.statements_created += 1

    await db.flush()

    # ---- Recalculate balances ----
    for local_acct_id in acct_id_map.values():
        acct = await db.get(Account, local_acct_id)
        if not acct:
            continue
        tx_sum = (await db.execute(
            select(sa_func.coalesce(sa_func.sum(Transaction.amount), Decimal("0.00")))
            .where(Transaction.account_id == acct.id)
        )).scalar()
        acct.current_balance = acct.initial_balance + tx_sum

    await db.flush()
    result.cat_id_map = cat_id_map
    return result


# ---------------------------------------------------------------------------
# Keyword suggestion engine
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "as", "was", "are", "be",
    "has", "had", "have", "do", "does", "did", "will", "can", "may",
    "payment", "purchase", "transaction", "debit", "credit", "pos",
    "direct", "deposit", "transfer", "automatic", "recurring",
    "ltd", "limited", "nz", "eft", "card", "visa",
}


def _tokenize(desc: str) -> list[str]:
    """Lowercase, strip numbers/punctuation, return tokens len > 2."""
    cleaned = re.sub(r"[0-9]+", "", desc.lower())
    return [
        tok
        for raw in cleaned.split()
        if len(tok := raw.strip(".,;:!?#*()-/& ")) > 2
    ]


def extract_keyword_suggestions(
    data: dict,
    cat_id_map: dict[str, uuid.UUID],
    account_ids: set[str] | None = None,
    skip_category_roots: set[str] | None = None,
    max_per_category: int = 8,
) -> list[dict]:
    """Analyse categorised transactions and suggest keywords per category.

    Returns a list of dicts:
        {
            "local_category_id": UUID,
            "category_name": str,
            "suggestions": [
                {"keyword": str, "count": int, "sample": str},
                ...
            ],
        }
    Only categories that were mapped (present in *cat_id_map*) are analysed.
    """
    skip_category_roots = skip_category_roots or set()
    ext = data.get("data", {})
    ext_cats = {c["category_id"]: c for c in ext.get("categories", [])}
    ext_txns = ext.get("transactions", [])

    root_ids_to_skip: set[str] = set()
    for c in ext_cats.values():
        if c["parent_category_id"] is None and c["category_name"] in skip_category_roots:
            root_ids_to_skip.add(c["category_id"])

    def _ancestor_root(cat_id: str | None) -> str | None:
        if cat_id is None:
            return None
        c = ext_cats.get(cat_id)
        if not c:
            return None
        if c["parent_category_id"] is None:
            return c["category_id"]
        return _ancestor_root(c["parent_category_id"])

    # Group descriptions by external category
    descs_by_cat: dict[str, list[str]] = defaultdict(list)
    for t in ext_txns:
        if account_ids and t["account_id"] not in account_ids:
            continue
        cid = t.get("category_id")
        if not cid or cid not in cat_id_map:
            continue
        root = _ancestor_root(cid)
        if root in root_ids_to_skip:
            continue
        descs_by_cat[cid].append(t["description"])

    if not descs_by_cat:
        return []

    # Build global token frequency across ALL categories for IDF-like filtering
    cat_presence: Counter[str] = Counter()
    token_by_cat: dict[str, Counter[str]] = {}
    bigram_by_cat: dict[str, Counter[str]] = {}
    sample_by_token: dict[str, dict[str, str]] = defaultdict(dict)

    total_cats = len(descs_by_cat)

    for cid, descriptions in descs_by_cat.items():
        tc: Counter[str] = Counter()
        bc: Counter[str] = Counter()
        seen_tokens: set[str] = set()

        for desc in descriptions:
            tokens = _tokenize(desc)
            for tok in tokens:
                if tok not in _STOP_WORDS:
                    tc[tok] += 1
                    if tok not in seen_tokens:
                        cat_presence[tok] += 1
                        seen_tokens.add(tok)
                    if tok not in sample_by_token[cid]:
                        sample_by_token[cid][tok] = desc[:80]
            for i in range(len(tokens) - 1):
                bg = f"{tokens[i]} {tokens[i + 1]}"
                if tokens[i] not in _STOP_WORDS or tokens[i + 1] not in _STOP_WORDS:
                    bc[bg] += 1
                    if bg not in sample_by_token[cid]:
                        sample_by_token[cid][bg] = desc[:80]

        token_by_cat[cid] = tc
        bigram_by_cat[cid] = bc

    # Score and rank candidates per category
    results = []
    for cid, descriptions in descs_by_cat.items():
        local_id = cat_id_map[cid]
        cat_name = ext_cats[cid]["category_name"]
        n_descs = len(descriptions)
        candidates: dict[str, float] = {}

        # Score bigrams (more specific, weighted higher)
        for bg, count in bigram_by_cat.get(cid, {}).items():
            if count < 2:
                continue
            candidates[bg] = count * 3.0

        # Score single tokens
        for tok, count in token_by_cat.get(cid, {}).items():
            if count < 2:
                continue
            presence_ratio = cat_presence[tok] / total_cats
            if presence_ratio > 0.5:
                continue
            specificity = 1.0 / max(cat_presence[tok], 1)
            candidates[tok] = count * specificity * 10

        # Deduplicate: if a bigram fully covers a single token, drop the token
        bigram_words = set()
        for bg in candidates:
            if " " in bg:
                bigram_words.update(bg.split())
        final: list[tuple[str, float, int]] = []
        for kw, score in candidates.items():
            if " " not in kw and kw in bigram_words:
                continue
            count = (
                bigram_by_cat.get(cid, {}).get(kw, 0)
                if " " in kw
                else token_by_cat.get(cid, {}).get(kw, 0)
            )
            final.append((kw, score, count))

        final.sort(key=lambda x: x[1], reverse=True)

        suggestions = [
            {
                "keyword": kw,
                "count": count,
                "sample": sample_by_token.get(cid, {}).get(kw, ""),
            }
            for kw, _, count in final[:max_per_category]
        ]

        if suggestions:
            results.append({
                "local_category_id": str(local_id),
                "category_name": cat_name,
                "transaction_count": n_descs,
                "suggestions": suggestions,
            })

    results.sort(key=lambda r: r["category_name"])
    return results


async def import_keywords(
    db: AsyncSession,
    user_id: uuid.UUID,
    keyword_selections: list[tuple[uuid.UUID, str]],
) -> int:
    """Create CategoryKeyword records from confirmed suggestions.

    *keyword_selections* is a list of (category_id, keyword) pairs.
    Skips duplicates where the keyword already exists for that category.
    Returns the count of keywords created.
    """
    created = 0
    for cat_id, keyword in keyword_selections:
        kw_lower = keyword.lower().strip()
        existing = (await db.execute(
            select(CategoryKeyword).where(
                CategoryKeyword.category_id == cat_id,
                CategoryKeyword.keyword == kw_lower,
            )
        )).scalar_one_or_none()
        if existing:
            continue
        db.add(CategoryKeyword(category_id=cat_id, keyword=kw_lower, hit_count=0))
        created += 1
    await db.flush()
    return created
