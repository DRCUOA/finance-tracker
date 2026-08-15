# Changelog

All notable changes to Finla are documented in this file.

Format: [Semantic Versioning](https://semver.org/) &mdash; `MAJOR.MINOR.PATCH`

---

## [Unreleased]

### Added
- `non_cash` category type for pure asset/liability value movements (interest accruals, revaluations, pension-fund growth) &mdash; excluded from every cash view: spending pulse, budgets, income-vs-expense totals and charts
- Interest job files accruals under a non-cash &ldquo;Interest&rdquo; category (created on demand) instead of posting them uncategorised into the spending pulse
- Default category seed includes a &ldquo;Non-Cash&rdquo; root with &ldquo;Interest&rdquo; child
- Alembic migration 023: adds the enum value and backfills &mdash; retypes &ldquo;Non-Cash Adjustments&rdquo; trees and &ldquo;Mortgage Interest&rdquo; to `non_cash`, reparents the latter under the non-cash root, and recategorises all `source='interest'` transactions
- Category filter on the Daily Allowance Used chart &mdash; tick categories in or out to see the pattern underneath an outlier. Bars, tooltips, the average-to-date line and the budget line all recompute from the selection (the budget line sums only the selected categories&rsquo; budgets), and a note names how many categories are hidden so a filtered chart is never mistaken for full spend. Runs entirely in the browser off the per-day breakdown already in the page &mdash; no reload
- &ldquo;Lock y-axis to busiest day&rdquo; option on the same chart &mdash; pins the scale to the highest single-day total across *all* categories, so bar heights stay comparable as categories are filtered in and out instead of rescaling to whatever is left. Locking also drops the broken-axis treatment, which exists to rescue a squashed axis and would otherwise fight the fixed scale
- **Statement Helper** &mdash; the standalone recon-helper tool, merged into the app at `/reconciliation/helper` (linked from the Reconciliation page). Drop the bank&rsquo;s OFX/QFX or CSV statement and it becomes a tickable list ordered like the reconcile screen (date ascending, statement order within a day), with a ticked total, remaining-to-tick, progress, an editable statement balance, a Find box that lights up lines of a given amount, and keyboard tick-off (&uarr;/&darr;, space, `/`). The file is parsed in the browser tab and never uploaded; ticks are saved on the device, keyed to the statement (bank FITIDs when present), and can be exported/imported as JSON &mdash; session files from the standalone tool import unchanged. CSV goes through a column-mapping step (signed amount or debit/credit columns, DD/MM vs MM/DD)
- **Split view on the reconcile screen** &mdash; a &ldquo;Statement helper&rdquo; toggle in the header opens the helper beside the transaction list, sticky and scrolling on its own, with a draggable divider (position and open/closed state remembered). While open: the helper&rsquo;s Find box follows the app&rsquo;s live difference, so whatever is still unaccounted for lights up on the statement side; the pinned bar shows both ticked totals and the gap between them (&ldquo;matches ✓&rdquo; once the same items are ticked on both sides); the helper warns if the balance in the file differs from the one the reconciliation was started with; and it resumes the last statement used for that account automatically. Below 1024px the helper stacks under the list instead

### Fixed
- Matching Rules preview no longer promises matches the rule can&rsquo;t deliver &mdash; it counted every uncategorised row, while saving skips reconciliation-locked ones, so a phrase could preview &ldquo;2 would match&rdquo; and change nothing. Preview and apply now read the same matcher, and the preview names the locked subset (&ldquo;2 would match. 2 of them are reconciled and won&rsquo;t be changed&rdquo;)
- **Hits** counts automatic matches. `hit_count` only moved when a user confirmed a categorisation by hand, so rules quietly categorising every import read `0` &mdash; indistinguishable from a rule that never matched, and enough to get them flagged &ldquo;zero-hit stale&rdquo; by the keyword health report. Statement import, Akahu sync and rule backfill now each record the hit against the keyword that won. Existing rules count from here on; they aren&rsquo;t backfilled
- Matching rules now run on CSV/OFX statement imports &mdash; previously only manual entry and the Akahu feed auto-categorised, so file-fed accounts never matched and rules looked broken for those accounts

### Changed
- Category selectors on the Transactions page &mdash; every row, the filter bar and the batch-categorise bar &mdash; are type-to-filter pickers instead of sixty-option native dropdowns. Open one and type: the list narrows to what matches. The text is tried as a case-insensitive regular expression first (`^ins`, `gas|water`, `hol.*trip`) against the category name and then its group, and falls back to a plain substring match while a pattern is still half-typed, so an unbalanced `(` never empties the list. Arrow keys move, Enter picks, Escape closes; typing on a focused picker opens it. The real `<select>` stays underneath, so form submission, the HTMX filter trigger and the locked-row confirmation all behave as before
- Saving a matching rule applies it immediately to existing uncategorised transactions (reconciliation-locked rows are left untouched and reported), instead of only affecting future ingests
- Matching Rules preview count applies the engine&rsquo;s word-boundary rule for phrases of four characters or fewer, so the preview no longer over-promises
- Import summary reports how many rows the rules categorised, linking to the review screen for the rest
- Auto-categoriser matches every category type, transfer and non-cash included &mdash; a statement line reading &ldquo;Online Payment &ndash; Thank You&rdquo; is a transfer, and refusing to match it only left it uncategorised, in the spending pulse&rsquo;s uncategorised bucket. Cash-basis reports still exclude these rows, by category type, however they were categorised
- Migration 007 skips cleanly on databases where its target user does not exist (fresh installs previously crashed mid-chain)

---

## [5.6.0] &ndash; 2026-04-13

### Added
- Partial clearing for commitments &mdash; record incremental payments without fully clearing
- Alembic migration 011: `cleared_amount` column on commitments table
- Rolling over/under report on spending page &mdash; cumulative budget vs actual spend with configurable start date
- Period-phase awareness in spending pulse &mdash; distinct messaging for current, past, and future periods

### Changed
- Spending page renamed from &ldquo;Live Position&rdquo; to **Budget Position**; daily chart renamed to &ldquo;Daily Allowance Used&rdquo;
- Spending actuals now filter by expense-category type instead of negative amounts, with refund offsets
- Commitments on spending page queried against the view period directly (no more month-level prorate)
- Commitment aggregations use outstanding balance (amount &minus; cleared_amount) instead of full amount
- Auto-categoriser excludes transfer-type categories from keyword suggestions
- Short keywords (&le;4 chars) use word-boundary matching to prevent false positives (e.g. &ldquo;on&rdquo; no longer matches inside &ldquo;loan&rdquo;)
- Keyword health report reflects word-boundary matching behaviour

---

## [5.5.0] &ndash; 2026-04-12

### Changed
- Rebranded from "Finance Tracker" to **Finla** across all pages, titles, and auth screens
- SVG logo mark — diagonal crossing-strands arrow motif (bottom-left hook → upper-right arrowhead) matching design brief orientation
- SVG favicon with carbon-black (#0a0a0a) rounded-rect and white mark
- Brand palette replaced: urban gray / charcoal carbon-black scale (#2d2d2d–#0a0a0a)
- Gradient backgrounds on body (gray-50→gray-100 / gray-900→brand-950), sidebar (brand-800→brand-950), and auth pages
- New semantic color tokens: `positive` (fluorescent green #00ff41 scale) and `negative` (orange-red #FF4500 scale)
- All success/positive UI (toasts, badges, lock states, imports, reports) migrated from emerald → positive
- All error/negative UI (toasts, alerts, value displays, lock states) migrated from red → negative
- Destructive buttons use outlined #FF4500 style instead of solid red
- Reusable button CSS classes: `.btn-primary`, `.btn-secondary`, `.btn-destructive`, `.btn-ghost`
- FastAPI app title updated to Finla

---

## [5.4.0] &ndash; 2026-04-12

### Added
- Commitments hub with full CRUD, stats strip, tab filtering, and inline editing
- Planning wizards: Monthly Bills Setup, Annual Expense Planner, Event/Project Budget
- Review History wizard — analyses past transactions to suggest recurring commitments
- Spending page integration showing commitments alongside actuals
- Commitment projection for recurring series (auto-generate upcoming instances)

### Fixed
- Review History wizard now only creates commitments for selected suggestions

---

## [5.3.16] &ndash; 2026-04-11

### Added
- Footer with copyright notice and build number across all pages
- CHANGELOG.md to track project history going forward

---

## [5.3.15] &ndash; 2026-04-10

### Added
- Help page with feature overview and keyboard shortcuts
- Backup/restore for categories and matching rules (separate endpoints)

### Changed
- Backup index page redesigned with card-based layout

---

## [5.3.14] &ndash; 2026-04-09

### Added
- Akahu bank-feed integration (app token + user token config)
- Bank Feeds page with sync controls and status display
- `akahu_id` and `akahu_account_id` fields on Account and Transaction models

### Changed
- `.env.example` updated with Akahu environment variables

---

## [5.3.13] &ndash; 2026-04-08

### Added
- Alembic migration 010: Akahu fields on accounts and transactions

---

## [5.3.12] &ndash; 2026-04-06

### Added
- Commitments & reserves model and service layer
- Alembic migration 009: commitments and reserves tables

---

## [5.3.11] &ndash; 2026-04-04

### Added
- `is_fixed` flag on categories for fixed-cost tracking
- Alembic migration 008: `is_fixed` column on categories

### Changed
- Reports overhauled with tabbed UI separating fixed vs discretionary spend
- Dashboard defaults improved for new installs

---

## [5.3.10] &ndash; 2026-04-02

### Fixed
- Data cleanup migration (007) correcting orphaned category references

---

## [5.3.9] &ndash; 2026-03-30

### Added
- Reconciliation draft support (save incomplete reconciliations)
- Alembic migration 006: reconciliation drafts table

---

## [5.3.8] &ndash; 2026-03-28

### Changed
- Reconciliation reworked to use ending balance instead of running totals
- Alembic migration 005: ending-balance reconciliation schema

---

## [5.3.7] &ndash; 2026-03-25

### Added
- Spending allocation breakdown on spending page

---

## [5.3.6] &ndash; 2026-03-22

### Added
- Keyword suggestion engine for auto-categorisation during import
- Migration flow improvements for uncategorised transaction handling

---

## [5.3.5] &ndash; 2026-03-20

### Added
- Confirmation dialogs for all destructive actions (delete account, delete category, delete transaction, etc.)

---

## [5.3.4] &ndash; 2026-03-18

### Added
- Coverage import feature on the dashboard
- Enhanced account details and dashboard display

---

## [5.3.3] &ndash; 2026-03-15

### Added
- Transaction deduplication index to prevent double-imports
- Improved transaction handling during CSV upload

---

## [5.3.2] &ndash; 2026-03-12

### Added
- Account term field (e.g. 12-month, revolving)
- Alembic migration 002: `term` column on accounts

---

## [5.3.1] &ndash; 2026-03-10

### Fixed
- Minor template rendering issues in import flow
- NZD currency filter edge case with None values

---

## [5.3.0] &ndash; 2026-03-08

### Added
- SQL Tool page for ad-hoc read-only queries against the database
- Syntax-highlighted results with copy-to-clipboard support

---

## [5.2.0] &ndash; 2026-03-04

### Added
- Reports module with monthly trends, category breakdown, and income-vs-expense charts
- Chart.js integration with custom doughnut hover animation plugin

---

## [5.1.0] &ndash; 2026-02-28

### Added
- Reconciliation workflow: mark periods as reconciled, lock transactions
- Padlock icons on reconciled transactions (locked = read-only)
- Transaction edit modal respects lock state

---

## [5.0.0] &ndash; 2026-02-22

### Changed
- Full UI redesign: icon-only sidebar with tooltips, Inter font, brand colour palette
- Dark mode support with system-preference detection and manual toggle
- All pages migrated to new Tailwind component system

### Removed
- Legacy Bootstrap-based layout

---

## [4.3.0] &ndash; 2026-02-15

### Added
- Matching rules engine for automatic transaction categorisation
- Rules management page (create, edit, delete, reorder)

---

## [4.2.0] &ndash; 2026-02-10

### Added
- Backup & restore system: full database export/import as JSON
- Backup page with download and upload controls

---

## [4.1.0] &ndash; 2026-02-05

### Added
- Category edit modal (inline editing from any page)
- Budget amount field on categories

---

## [4.0.0] &ndash; 2026-01-30

### Added
- Transaction edit modal with full field editing
- Inline category and account selectors with grouped options

### Changed
- Transaction list page redesigned with filter bar and pagination

---

## [3.5.0] &ndash; 2026-01-24

### Added
- Spending page with doughnut chart and period selector
- NZD currency formatting filter

---

## [3.4.0] &ndash; 2026-01-20

### Added
- CSV import with column mapping and preview step
- ASB, Kiwibank, and Westpac CSV format support

---

## [3.3.0] &ndash; 2026-01-16

### Added
- Migration upload page for importing from legacy finance apps
- Uncategorised transaction handling in migration flow

---

## [3.2.0] &ndash; 2026-01-12

### Added
- Categories management page with parent/child hierarchy
- Category types: income, expense, transfer

---

## [3.1.0] &ndash; 2026-01-08

### Added
- Accounts management page (create, edit, delete)
- Account types: cheque, savings, credit card, loan, investment
- `is_cashflow` flag for including/excluding accounts from reports

---

## [3.0.0] &ndash; 2026-01-04

### Added
- Dashboard with account balances summary and recent transactions
- Quick stats cards (total balance, monthly income, monthly expenses)

### Changed
- Jinja2 templating layer extracted into `app/templating.py`

---

## [2.2.0] &ndash; 2025-12-28

### Added
- Refresh token support (7-day expiry)
- Secure cookie-based session handling

---

## [2.1.0] &ndash; 2025-12-22

### Added
- User authentication with JWT access tokens
- Login and logout pages
- Protected route middleware

---

## [2.0.0] &ndash; 2025-12-18

### Changed
- Migrated from SQLite to PostgreSQL with asyncpg
- Alembic migration framework initialised
- Migration 001: initial schema (users, accounts, transactions, categories)

---

## [1.3.0] &ndash; 2025-12-12

### Added
- Toast notification system (success, warning, error)
- Alpine.js reactive toast container

---

## [1.2.0] &ndash; 2025-12-08

### Added
- HTMX integration for partial page updates
- Loading spinners on async operations

---

## [1.1.0] &ndash; 2025-12-04

### Added
- Static file serving
- Tailwind CSS via CDN
- Base HTML template with responsive layout

---

## [1.0.0] &ndash; 2025-12-01

### Added
- FastAPI application scaffold
- Project structure: routers, models, services, templates
- Health-check endpoint
- Pydantic settings with `.env` file support

---

## [0.2.0] &ndash; 2025-11-26

### Added
- SQLAlchemy async ORM models (User, Account, Transaction, Category)
- Database session management

---

## [0.1.0] &ndash; 2025-11-22

### Added
- Python virtual environment and dependency management
- `requirements.txt` with pinned versions
- Project README

---

## [0.0.1] &ndash; 2025-11-20

### Added
- Repository initialised
- `.gitignore` and basic project scaffolding
