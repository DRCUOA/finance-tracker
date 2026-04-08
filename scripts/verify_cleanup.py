"""Post-migration verification for data cleanup (Alembic 007).

Connects to the database and asserts exact expected results for every
deterministic migration change. Exits non-zero if any assertion fails.

Usage:
    source .venv/bin/activate
    python scripts/verify_cleanup.py
"""

import asyncio
import sys
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

UID = "a6beaf68-88fd-4e8e-8dfc-6919a631456a"

passed = 0
failed = 0


def check(description: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {description}")
    else:
        failed += 1
        msg = f"  FAIL  {description}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


async def run_checks():
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.connect() as conn:

        async def val(query, params=None):
            r = await conn.execute(text(query), params or {"uid": UID})
            return r.fetchone()

        async def vals(query, params=None):
            r = await conn.execute(text(query), params or {"uid": UID})
            return r.fetchall()

        async def scalar(query, params=None):
            r = await val(query, params)
            return r[0] if r else None

        # ── Account assertions ───────────────────────────────────────
        print("\n=== Account assertions ===")

        accts = {
            "ANZ Home Loan": {"is_cashflow": False},
            "QROPS Craigs - Moira": {"account_type": "investment"},
            "KtCar Loan": {"account_type": "loan", "is_cashflow": False},
            "Kiwisaver Moi": {"is_cashflow": False},
            "Kiwisaver Rich": {"is_cashflow": False},
        }
        for name, expected in accts.items():
            row = await val(
                "SELECT account_type, is_cashflow FROM accounts "
                "WHERE name = :name AND user_id = :uid",
                {"name": name, "uid": UID},
            )
            if not row:
                check(f"{name} exists", False, "account not found")
                continue
            for col, exp_val in expected.items():
                actual = row._mapping[col]
                check(
                    f"{name}.{col} = {exp_val!r}",
                    actual == exp_val,
                    f"got {actual!r}",
                )

        # ── Category name assertions ─────────────────────────────────
        print("\n=== Category name assertions ===")

        old_names = ["Discreationary Spending", "Books & Stationary", "Work Expenses & Reembursments"]
        new_names = ["Discretionary Spending", "Books & Stationery", "Work Expenses & Reimbursements"]

        for old in old_names:
            cnt = await scalar(
                "SELECT count(*) FROM categories WHERE name = :n AND user_id = :uid",
                {"n": old, "uid": UID},
            )
            check(f"No category named '{old}'", cnt == 0, f"found {cnt}")

        for new in new_names:
            cnt = await scalar(
                "SELECT count(*) FROM categories WHERE name = :n AND user_id = :uid",
                {"n": new, "uid": UID},
            )
            check(f"Category '{new}' exists", cnt == 1, f"found {cnt}")

        # ── Category type assertions ─────────────────────────────────
        print("\n=== Category type assertions ===")

        type_checks = {
            "Income Tax Refunds": "income",
            "House": "transfer",
            "Pension Fund Moi": "transfer",
            "Pension Fund Rich": "transfer",
        }
        for cat_name, exp_type in type_checks.items():
            actual = await scalar(
                "SELECT category_type FROM categories WHERE name = :n AND user_id = :uid",
                {"n": cat_name, "uid": UID},
            )
            check(f"{cat_name}.category_type = '{exp_type}'", actual == exp_type, f"got {actual!r}")

        # ── New category existence assertions ────────────────────────
        print("\n=== New category existence assertions ===")

        new_cats = [
            ("Mortgage Interest", "expense", "Housing"),
            ("Loan Principal Transfer", "transfer", "Transfers"),
            ("Loan Drawdown / Borrowing", "transfer", "Transfers"),
            ("Non-Cash Adjustments", "transfer", None),
            ("Work Reimbursements", "income", "Income"),
        ]
        for cat_name, exp_type, exp_parent in new_cats:
            row = await val(
                "SELECT c.category_type, p.name as parent_name "
                "FROM categories c "
                "LEFT JOIN categories p ON c.parent_id = p.id "
                "WHERE c.name = :n AND c.user_id = :uid",
                {"n": cat_name, "uid": UID},
            )
            if not row:
                check(f"{cat_name} exists", False, "not found")
                continue
            check(f"{cat_name} exists", True)
            check(
                f"{cat_name}.category_type = '{exp_type}'",
                row[0] == exp_type,
                f"got {row[0]!r}",
            )
            check(
                f"{cat_name}.parent = {exp_parent!r}",
                row[1] == exp_parent,
                f"got {row[1]!r}",
            )

        # ── Category re-parenting assertions ─────────────────────────
        print("\n=== Category re-parenting assertions ===")

        for child_name in ("House", "Pension Fund Moi", "Pension Fund Rich"):
            parent_name = await scalar(
                "SELECT p.name FROM categories c "
                "JOIN categories p ON c.parent_id = p.id "
                "WHERE c.name = :n AND c.user_id = :uid",
                {"n": child_name, "uid": UID},
            )
            check(
                f"{child_name}.parent = 'Non-Cash Adjustments'",
                parent_name == "Non-Cash Adjustments",
                f"got {parent_name!r}",
            )

        # ── Keyword removal assertions ───────────────────────────────
        print("\n=== Keyword removal assertions ===")

        bad_kws = ["albany", "wellington", "clark", "new zealand", "central"]
        for kw in bad_kws:
            cnt = await scalar(
                "SELECT count(*) FROM category_keywords ck "
                "JOIN categories c ON ck.category_id = c.id "
                "WHERE c.user_id = :uid AND ck.keyword = :kw",
                {"kw": kw, "uid": UID},
            )
            check(f"No keyword '{kw}' exists", cnt == 0, f"found {cnt}")

        # ── Keyword placement assertions ─────────────────────────────
        print("\n=== Keyword placement assertions ===")

        kw_placements = {
            "loan interest": "Mortgage Interest",
            "loan payment": "Loan Principal Transfer",
            "loan drawdown": "Loan Drawdown / Borrowing",
        }
        for kw, exp_cat in kw_placements.items():
            actual_cat = await scalar(
                "SELECT c.name FROM category_keywords ck "
                "JOIN categories c ON ck.category_id = c.id "
                "WHERE c.user_id = :uid AND ck.keyword = :kw",
                {"kw": kw, "uid": UID},
            )
            check(f"Keyword '{kw}' -> {exp_cat}", actual_cat == exp_cat, f"got {actual_cat!r}")

        # ── Transaction recategorization assertions ──────────────────
        print("\n=== Transaction recategorization assertions ===")

        # Mobile: no non-phone transactions remain
        non_phone_in_mobile = await scalar(
            "SELECT count(*) FROM transactions t "
            "JOIN categories c ON t.category_id = c.id "
            "WHERE c.name = 'Mobile' AND c.user_id = :uid "
            "AND LOWER(t.description) NOT LIKE '%%vodafone%%' "
            "AND LOWER(t.description) NOT LIKE '%%one nz%%' "
            "AND LOWER(t.description) NOT LIKE '%%2degrees%%' "
            "AND t.user_id = :uid",
        )
        check("Zero non-phone transactions in Mobile", non_phone_in_mobile == 0, f"found {non_phone_in_mobile}")

        # Ubers: no uber eats transactions remain
        uber_eats_in_ubers = await scalar(
            "SELECT count(*) FROM transactions t "
            "JOIN categories c ON t.category_id = c.id "
            "WHERE c.name = 'Ubers' AND c.user_id = :uid "
            "AND LOWER(t.description) LIKE '%%uber eats%%' "
            "AND t.user_id = :uid",
        )
        check("Zero Uber Eats transactions in Ubers", uber_eats_in_ubers == 0, f"found {uber_eats_in_ubers}")

        # ANZ Home Loan: loan interest -> Mortgage Interest
        li_wrong = await scalar(
            "SELECT count(*) FROM transactions t "
            "LEFT JOIN categories c ON t.category_id = c.id "
            "WHERE t.account_id = (SELECT id FROM accounts WHERE name = 'ANZ Home Loan' AND user_id = :uid) "
            "AND LOWER(t.description) LIKE '%%loan interest%%' "
            "AND (c.name IS NULL OR c.name != 'Mortgage Interest') "
            "AND t.user_id = :uid",
        )
        check("All 'loan interest' txs in Mortgage Interest", li_wrong == 0, f"{li_wrong} not in correct category")

        # ANZ Home Loan: loan payment -> Loan Principal Transfer
        lp_wrong = await scalar(
            "SELECT count(*) FROM transactions t "
            "LEFT JOIN categories c ON t.category_id = c.id "
            "WHERE t.account_id = (SELECT id FROM accounts WHERE name = 'ANZ Home Loan' AND user_id = :uid) "
            "AND LOWER(t.description) LIKE '%%loan payment%%' "
            "AND (c.name IS NULL OR c.name != 'Loan Principal Transfer') "
            "AND t.user_id = :uid",
        )
        check("All 'loan payment' txs in Loan Principal Transfer", lp_wrong == 0, f"{lp_wrong} not in correct category")

        # ANZ Home Loan: loan drawdown -> Loan Drawdown / Borrowing
        ld_wrong = await scalar(
            "SELECT count(*) FROM transactions t "
            "LEFT JOIN categories c ON t.category_id = c.id "
            "WHERE t.account_id = (SELECT id FROM accounts WHERE name = 'ANZ Home Loan' AND user_id = :uid) "
            "AND LOWER(t.description) LIKE '%%loan drawdown%%' "
            "AND (c.name IS NULL OR c.name != 'Loan Drawdown / Borrowing') "
            "AND t.user_id = :uid",
        )
        check("All 'loan drawdown' txs in Loan Drawdown / Borrowing", ld_wrong == 0, f"{ld_wrong} not in correct category")

        # ── Reporting integrity assertions ───────────────────────────
        print("\n=== Reporting integrity assertions ===")

        # Mortgage Interest category is expense type
        mi_type = await scalar(
            "SELECT category_type FROM categories WHERE name = 'Mortgage Interest' AND user_id = :uid",
        )
        check("Mortgage Interest is 'expense' type", mi_type == "expense", f"got {mi_type!r}")

        # Loan Principal Transfer is transfer type (not expense)
        lpt_type = await scalar(
            "SELECT category_type FROM categories WHERE name = 'Loan Principal Transfer' AND user_id = :uid",
        )
        check("Loan Principal Transfer is 'transfer' type", lpt_type == "transfer", f"got {lpt_type!r}")

        # Loan Drawdown / Borrowing is transfer type (not expense or income)
        ldb_type = await scalar(
            "SELECT category_type FROM categories WHERE name = 'Loan Drawdown / Borrowing' AND user_id = :uid",
        )
        check("Loan Drawdown / Borrowing is 'transfer' type", ldb_type == "transfer", f"got {ldb_type!r}")

        # House/Pension transactions are under transfer-type categories
        for cat_name in ("House", "Pension Fund Moi", "Pension Fund Rich"):
            wrong = await scalar(
                "SELECT count(*) FROM transactions t "
                "JOIN categories c ON t.category_id = c.id "
                "WHERE c.name = :cn AND c.user_id = :uid AND c.category_type != 'transfer'",
                {"cn": cat_name, "uid": UID},
            )
            check(f"{cat_name} transactions under transfer-type category", wrong == 0, f"{wrong} in non-transfer")

    await engine.dispose()


def main():
    asyncio.run(run_checks())
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
