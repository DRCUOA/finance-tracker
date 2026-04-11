# Changelog

All notable changes to Finance Tracker are documented in this file.

Format: [Semantic Versioning](https://semver.org/) &mdash; `MAJOR.MINOR.PATCH`

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
