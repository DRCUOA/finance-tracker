# Finance Tracker Backup Review

Source file: finance-tracker-backup080420261354.json
Records reviewed: 12 accounts, 72 categories, 2345 transactions.

## Executive view

- The biggest accuracy problem is mortgage handling: loan drawdown, loan principal movements, and loan interest are mixed together in spending categories.
- The biggest simplicity problem is keyword sprawl: many keywords are broad, location-based, or stale, and they are causing misclassification.
- Long-term asset revaluations are being treated as expenses, which distorts spending reports.

## Highest-priority fixes

1. Reclassify home-loan drawdown (`Loan Drawdown`) out of `Debit Interest`.
2. Split mortgage into:
  - `Mortgage Interest` (expense)
  - `Mortgage Principal / Loan Transfer` (transfer or liability movement, not expense)
3. Move house and pension valuation movements out of expense reporting into a dedicated non-spend bucket.
4. Remove broad keywords such as `albany`, `wellington`, `clark`, `new zealand`, `central`.
5. Split work wages from reimbursements and one-off gifts.

## Specific findings

### Accounts

- `QROPS CRAIGS - Richard` is `investment`, but `QROPS Craigs - Moira` is `other`. These should likely use the same account type.
- `KtCar Loan` is typed as `investment`, which is inconsistent with its name; it likely belongs under `loan` or another liability/receivable type.
- `Kiwisaver Moi` and `Kiwisaver Rich` are marked `is_cashflow=true`, which will usually pollute cashflow reporting if these are long-term investment accounts.

### Categories and naming

- Misspellings: `Discreationary Spending` → `Discretionary Spending`; `Books & Stationary` → `Books & Stationery`; `Work Expenses & Reembursments` → `Work Expenses & Reimbursements`.
- `Income Tax Refunds` is typed as `expense`; this should normally be `income` or a contra-tax treatment.
- `House`, `Pension Fund Moi`, and `Pension Fund Rich` sit under `Long Term Asset Movements` but are typed as `expense`. That will distort true household spending.
- There are several unused leaf categories: `GEM Card fees`, `Other Electronics`, `Income Tax Refunds`, `Life Assurance`, and `Miscellaneous Discretionary`.

### Keyword problems

- There are 91 keywords with `hit_count=0`; many look stale or speculative.
- Exact duplicate keywords across categories include `annual card fee`, `debit aianz`, and `albany auckland`.
- Broad or location-based keywords are causing cross-category collisions:
  - `albany` in `Mobile`
  - `wellington` in `Discretionary Spending`
  - `clark` in `Katie`
  - `new zealand` in `Health Insurance`

### Transaction misclassification examples

- `Mobile`: 21 of 43 transactions do not mention Vodafone/One NZ and appear to be miscoded because of the keyword `albany`.
- Examples include KFC Albany, Peter Alexander, Pak N Save Albany, Cotton On, ATM withdrawals, and even a `Loan Payment`.
- `Ubers`: 5 transactions are `Uber Eats`, which are food delivery, not transport.
- `Discretionary Spending`: at least 8 cinema transactions and many food/drink transactions appear to be routed there because of the keyword `wellington`, despite more specific categories existing.
- `Work Expenses & Reimbursements` contains wages, reimbursements, gifts, travel, and other one-offs. This mixes salary/income logic with reimbursable outgoings and social contributions.

### Mortgage accuracy problem

- `Mortgage` currently has 462 transactions with a net total of 154,567.81.
- On the loan account alone, `Loan Interest` totals -210,221.85 and `Loan Payment` totals 435,752.66.
- On `ANZ Current`, mortgage payments total -69,990.00.
- This strongly suggests principal movements and spending are being mixed together, and may be double-counting mortgage activity in reports.

### Long-term valuation movements

- `House` contains monthly valuation adjustments on the `House Asset` account. These belong in net-worth tracking, not spending.
- `Pension Fund Moi` appears suspiciously flat for long stretches (mostly ~3,602.89 each month) and should be checked against source statements.
- `Pension Fund Rich` is much more variable. The difference in pattern suggests the Moira series deserves verification.

## Recommended target model

### Keep spending view simple

- Spending = only real out-of-pocket consumption and true costs.
- Transfers = account-to-account movements, card repayments, loan principal, savings sweeps.
- Net worth adjustments = house valuation changes, pension valuation changes, other asset revaluations.

### Suggested category changes

- Add `Mortgage Interest` under Housing (expense).
- Add `Loan Principal Transfer` under Transfers (transfer).
- Add `Asset Revaluations` or `Net Worth Adjustments` for house and pension revaluations.
- Split `Work Expenses & Reimbursements` into:
  - `Salary / Wages`
  - `Work Reimbursements`
  - `Work Out-of-Pocket`
  - `Gifts / Social Collections` (if you want to track these separately)
- Split `Ubers` into `Rideshare` and `Food Delivery`, or classify Uber Eats into `Takeaways`.
- Merge or simplify overlapping food categories if desired: `Restaurants`, `Cafe & Coffee`, `Takeaways and Junk Food`.

## Cleanup order

1. Fix mortgage and transfer logic.
2. Move valuation movements out of expenses.
3. Remove broad keywords and stale zero-hit keywords.
4. Correct obvious account and category type mismatches.
5. Rename misspelled categories.
6. Review unused leaf categories and delete/archive the dead ones.