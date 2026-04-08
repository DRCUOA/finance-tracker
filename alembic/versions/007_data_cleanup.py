"""Data cleanup: account fixes, category corrections, keyword removal, transaction recategorization

Backup-first, forward-only data cleanup migration. No downgrade implemented;
restore from backup if rollback is required.

Revision ID: 007
Revises: 006
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UID = "a6beaf68-88fd-4e8e-8dfc-6919a631456a"


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1a. Account fixes ────────────────────────────────────────────
    conn.execute(sa.text(
        "UPDATE accounts SET is_cashflow = false "
        "WHERE name = 'ANZ Home Loan' AND user_id = :uid"
    ), {"uid": UID})

    conn.execute(sa.text(
        "UPDATE accounts SET account_type = 'investment' "
        "WHERE name = 'QROPS Craigs - Moira' AND user_id = :uid"
    ), {"uid": UID})

    conn.execute(sa.text(
        "UPDATE accounts SET account_type = 'loan', is_cashflow = false "
        "WHERE name = 'KtCar Loan' AND user_id = :uid"
    ), {"uid": UID})

    conn.execute(sa.text(
        "UPDATE accounts SET is_cashflow = false "
        "WHERE name ILIKE 'Kiwisaver%%' AND user_id = :uid"
    ), {"uid": UID})

    # ── 1b. Category spelling corrections ────────────────────────────
    renames = [
        ("Discreationary Spending", "Discretionary Spending"),
        ("Books & Stationary", "Books & Stationery"),
        ("Work Expenses & Reembursments", "Work Expenses & Reimbursements"),
    ]
    for old_name, new_name in renames:
        conn.execute(sa.text(
            "UPDATE categories SET name = :new_name "
            "WHERE name = :old_name AND user_id = :uid"
        ), {"old_name": old_name, "new_name": new_name, "uid": UID})

    # ── 1c. Category type corrections ────────────────────────────────
    type_fixes = [
        ("Income Tax Refunds", "income"),
        ("House", "transfer"),
        ("Pension Fund Moi", "transfer"),
        ("Pension Fund Rich", "transfer"),
    ]
    for cat_name, new_type in type_fixes:
        conn.execute(sa.text(
            "UPDATE categories SET category_type = :new_type "
            "WHERE name = :name AND user_id = :uid"
        ), {"name": cat_name, "new_type": new_type, "uid": UID})

    # ── 1d. New categories ───────────────────────────────────────────

    # Helper: look up parent id by name
    def parent_id(name):
        row = conn.execute(sa.text(
            "SELECT id FROM categories "
            "WHERE name = :name AND parent_id IS NULL AND user_id = :uid"
        ), {"name": name, "uid": UID}).fetchone()
        return row[0] if row else None

    # Helper: get max sort_order for children of a parent
    def next_sort(pid):
        row = conn.execute(sa.text(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories "
            "WHERE parent_id = :pid AND user_id = :uid"
        ), {"pid": str(pid) if pid else None, "uid": UID}).fetchone()
        return row[0] if row else 0

    # Helper: get max sort_order for top-level categories
    def next_top_sort():
        row = conn.execute(sa.text(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories "
            "WHERE parent_id IS NULL AND user_id = :uid"
        ), {"uid": UID}).fetchone()
        return row[0] if row else 0

    housing_id = parent_id("Housing")
    transfers_id = parent_id("Transfers")
    income_id = parent_id("Income")

    # 1. Mortgage Interest (child of Housing, expense)
    conn.execute(sa.text(
        "INSERT INTO categories (id, user_id, parent_id, name, category_type, sort_order, budgeted_amount, created_at) "
        "VALUES (gen_random_uuid(), :uid, :pid, 'Mortgage Interest', 'expense', :sort, 0, NOW())"
    ), {"uid": UID, "pid": str(housing_id), "sort": next_sort(housing_id)})

    # 2. Loan Principal Transfer (child of Transfers, transfer)
    conn.execute(sa.text(
        "INSERT INTO categories (id, user_id, parent_id, name, category_type, sort_order, budgeted_amount, created_at) "
        "VALUES (gen_random_uuid(), :uid, :pid, 'Loan Principal Transfer', 'transfer', :sort, 0, NOW())"
    ), {"uid": UID, "pid": str(transfers_id), "sort": next_sort(transfers_id)})

    # 3. Loan Drawdown / Borrowing (child of Transfers, transfer)
    conn.execute(sa.text(
        "INSERT INTO categories (id, user_id, parent_id, name, category_type, sort_order, budgeted_amount, created_at) "
        "VALUES (gen_random_uuid(), :uid, :pid, 'Loan Drawdown / Borrowing', 'transfer', :sort, 0, NOW())"
    ), {"uid": UID, "pid": str(transfers_id), "sort": next_sort(transfers_id)})

    # 4. Non-Cash Adjustments (top-level, transfer)
    conn.execute(sa.text(
        "INSERT INTO categories (id, user_id, parent_id, name, category_type, sort_order, budgeted_amount, created_at) "
        "VALUES (gen_random_uuid(), :uid, NULL, 'Non-Cash Adjustments', 'transfer', :sort, 0, NOW())"
    ), {"uid": UID, "sort": next_top_sort()})

    # Re-parent House, Pension Fund Moi, Pension Fund Rich under Non-Cash Adjustments
    nca_id_row = conn.execute(sa.text(
        "SELECT id FROM categories WHERE name = 'Non-Cash Adjustments' AND user_id = :uid"
    ), {"uid": UID}).fetchone()
    nca_id = nca_id_row[0]

    for child_name in ("House", "Pension Fund Moi", "Pension Fund Rich"):
        conn.execute(sa.text(
            "UPDATE categories SET parent_id = :nca_id "
            "WHERE name = :name AND user_id = :uid"
        ), {"nca_id": str(nca_id), "name": child_name, "uid": UID})

    # 5. Work Reimbursements (child of Income, income)
    conn.execute(sa.text(
        "INSERT INTO categories (id, user_id, parent_id, name, category_type, sort_order, budgeted_amount, created_at) "
        "VALUES (gen_random_uuid(), :uid, :pid, 'Work Reimbursements', 'income', :sort, 0, NOW())"
    ), {"uid": UID, "pid": str(income_id), "sort": next_sort(income_id)})

    # ── Move keywords to new categories ──────────────────────────────
    # loan interest -> Mortgage Interest
    mortgage_interest_row = conn.execute(sa.text(
        "SELECT id FROM categories WHERE name = 'Mortgage Interest' AND user_id = :uid"
    ), {"uid": UID}).fetchone()
    mortgage_interest_id = mortgage_interest_row[0]

    conn.execute(sa.text(
        "UPDATE category_keywords SET category_id = :new_cat "
        "WHERE keyword = 'loan interest' "
        "AND category_id IN (SELECT id FROM categories WHERE user_id = :uid)"
    ), {"new_cat": str(mortgage_interest_id), "uid": UID})

    # loan payment -> Loan Principal Transfer
    lpt_row = conn.execute(sa.text(
        "SELECT id FROM categories WHERE name = 'Loan Principal Transfer' AND user_id = :uid"
    ), {"uid": UID}).fetchone()
    lpt_id = lpt_row[0]

    conn.execute(sa.text(
        "UPDATE category_keywords SET category_id = :new_cat "
        "WHERE keyword = 'loan payment' "
        "AND category_id IN (SELECT id FROM categories WHERE user_id = :uid)"
    ), {"new_cat": str(lpt_id), "uid": UID})

    # loan drawdown -> Loan Drawdown / Borrowing (create keyword if not exists)
    ldb_row = conn.execute(sa.text(
        "SELECT id FROM categories WHERE name = 'Loan Drawdown / Borrowing' AND user_id = :uid"
    ), {"uid": UID}).fetchone()
    ldb_id = ldb_row[0]

    existing_ld_kw = conn.execute(sa.text(
        "SELECT id FROM category_keywords "
        "WHERE keyword = 'loan drawdown' "
        "AND category_id IN (SELECT id FROM categories WHERE user_id = :uid)"
    ), {"uid": UID}).fetchone()

    if existing_ld_kw:
        conn.execute(sa.text(
            "UPDATE category_keywords SET category_id = :new_cat "
            "WHERE keyword = 'loan drawdown' "
            "AND category_id IN (SELECT id FROM categories WHERE user_id = :uid)"
        ), {"new_cat": str(ldb_id), "uid": UID})
    else:
        conn.execute(sa.text(
            "INSERT INTO category_keywords (id, category_id, keyword, hit_count, created_at) "
            "VALUES (gen_random_uuid(), :cat_id, 'loan drawdown', 0, NOW())"
        ), {"cat_id": str(ldb_id)})

    # ── 1e. Keyword cleanup ──────────────────────────────────────────
    # Remove known-bad broad/location keywords
    conn.execute(sa.text(
        "DELETE FROM category_keywords "
        "WHERE keyword IN ('albany', 'wellington', 'clark', 'new zealand', 'central') "
        "AND category_id IN (SELECT id FROM categories WHERE user_id = :uid)"
    ), {"uid": UID})

    # Remove stale zero-hit keywords older than 90 days
    conn.execute(sa.text(
        "DELETE FROM category_keywords "
        "WHERE hit_count = 0 "
        "AND created_at < NOW() - INTERVAL '90 days' "
        "AND category_id IN (SELECT id FROM categories WHERE user_id = :uid)"
    ), {"uid": UID})

    # ── 1f. Transaction recategorization ─────────────────────────────
    # Mobile misclassifications: uncategorize non-phone transactions
    conn.execute(sa.text(
        "UPDATE transactions SET category_id = NULL "
        "WHERE category_id = (SELECT id FROM categories WHERE name = 'Mobile' AND user_id = :uid) "
        "AND LOWER(description) NOT LIKE '%%vodafone%%' "
        "AND LOWER(description) NOT LIKE '%%one nz%%' "
        "AND LOWER(description) NOT LIKE '%%2degrees%%' "
        "AND user_id = :uid"
    ), {"uid": UID})

    # Uber Eats -> Takeaways and Junk Food
    conn.execute(sa.text(
        "UPDATE transactions SET category_id = ("
        "  SELECT id FROM categories WHERE name = 'Takeaways and Junk Food' AND user_id = :uid"
        ") "
        "WHERE category_id = (SELECT id FROM categories WHERE name = 'Ubers' AND user_id = :uid) "
        "AND LOWER(description) LIKE '%%uber eats%%' "
        "AND user_id = :uid"
    ), {"uid": UID})

    # Mortgage split on ANZ Home Loan account:
    # loan interest -> Mortgage Interest
    conn.execute(sa.text(
        "UPDATE transactions SET category_id = ("
        "  SELECT id FROM categories WHERE name = 'Mortgage Interest' AND user_id = :uid"
        ") "
        "WHERE account_id = (SELECT id FROM accounts WHERE name = 'ANZ Home Loan' AND user_id = :uid) "
        "AND LOWER(description) LIKE '%%loan interest%%' "
        "AND user_id = :uid"
    ), {"uid": UID})

    # loan payment (including reversals) -> Loan Principal Transfer
    conn.execute(sa.text(
        "UPDATE transactions SET category_id = ("
        "  SELECT id FROM categories WHERE name = 'Loan Principal Transfer' AND user_id = :uid"
        ") "
        "WHERE account_id = (SELECT id FROM accounts WHERE name = 'ANZ Home Loan' AND user_id = :uid) "
        "AND LOWER(description) LIKE '%%loan payment%%' "
        "AND user_id = :uid"
    ), {"uid": UID})

    # loan drawdown -> Loan Drawdown / Borrowing
    conn.execute(sa.text(
        "UPDATE transactions SET category_id = ("
        "  SELECT id FROM categories WHERE name = 'Loan Drawdown / Borrowing' AND user_id = :uid"
        ") "
        "WHERE account_id = (SELECT id FROM accounts WHERE name = 'ANZ Home Loan' AND user_id = :uid) "
        "AND LOWER(description) LIKE '%%loan drawdown%%' "
        "AND user_id = :uid"
    ), {"uid": UID})


def downgrade() -> None:
    raise NotImplementedError(
        "This is a forward-only data cleanup migration. "
        "Restore from backup if rollback is required."
    )
