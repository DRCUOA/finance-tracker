# Finance Tracker

A personal finance management app that lets households track accounts, import bank statements (CSV/OFX), categorise transactions, and reconcile them against their records. Provides budgeting, reporting, and net worth visibility.

## Features

- **Authentication** — Secure per-user data with JWT access + refresh tokens in httpOnly cookies
- **Accounts** — Create and manage financial accounts (checking, savings, credit card, loan, investment) with running balances
- **Transactions** — Full CRUD with search, filter, sort, and batch operations
- **CSV/OFX Import** — Upload bank files, preview data, map fields, and deduplicate on import
- **Categories** — Hierarchical user-defined categories with budgeted amounts
- **Auto-Categorisation** — Keyword-based category suggestions with feedback learning
- **Statement Reconciliation** — Three-pass matching engine (exact, keyword, fuzzy) with manual override
- **Reporting** — Monthly summaries, budget vs actual, category averages, net worth history, spending breakdowns
- **Net Worth Dashboard** — Assets, liabilities, net worth at a glance with trend chart
- **Data Export/Backup** — Full JSON backup and restore, plus CSV/JSON table-level export

## Tech Stack

- **Backend:** Python / FastAPI
- **Templates:** Jinja2 with HTMX, Alpine.js, Tailwind CSS
- **Database:** PostgreSQL via SQLAlchemy (async) + Alembic
- **Charts:** Chart.js
- **Auth:** JWT (python-jose) + bcrypt (passlib)

## Quick Start (Docker)

```bash
# Clone and start
docker compose up --build

# The app is available at http://localhost:8000
```

## Local Development

```bash
# Create a virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your database URL and secret key

# Start PostgreSQL (e.g. via Docker)
docker compose up db -d

# Run migrations
alembic upgrade head

# Start the dev server
uvicorn app.main:app --reload --port 8000
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string (asyncpg) | `postgresql+asyncpg://finance:finance@localhost:5432/finance_tracker` |
| `SECRET_KEY` | JWT signing key (use a long random string) | `change-me-to-a-long-random-string` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access token lifetime | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh token lifetime | `7` |

## Project Structure

```
app/
├── main.py              # FastAPI app entry point
├── config.py            # Settings from environment
├── database.py          # Async SQLAlchemy setup
├── models/              # ORM models
├── schemas/             # Pydantic schemas
├── routers/             # Route handlers
├── services/            # Business logic
├── templates/           # Jinja2 HTML templates
└── static/              # Static assets
```
