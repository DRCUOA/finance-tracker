#!/usr/bin/env bash
set -euo pipefail

# ── Colours & helpers ────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

pass()  { printf "  ${GREEN}✔${NC}  %s\n" "$*"; }
warn()  { printf "  ${YELLOW}⚠${NC}  %s\n" "$*"; }
fail()  { printf "  ${RED}✖${NC}  %s\n" "$*"; }
info()  { printf "  ${CYAN}→${NC}  %s\n" "$*"; }
header(){ printf "\n${BOLD}%s${NC}\n" "$*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
PORT="${PORT:-8000}"
ERRORS=0

header "Finla — pre-flight checks"

# ── 1. Python ────────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    pass "Python found: $PY_VER"
else
    fail "python3 not found — install Python 3.11+ first"
    exit 1
fi

# ── 2. Virtual environment ──────────────────────────────────────────
if [[ -d "$VENV_DIR" ]]; then
    pass "Virtual environment exists at $VENV_DIR"
else
    warn "Virtual environment not found — creating one"
    python3 -m venv "$VENV_DIR"
    pass "Created $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pass "Activated venv ($(python --version))"

# ── 3. pip dependencies ─────────────────────────────────────────────
header "Checking Python packages"

MISSING_PKGS=()
CORE_IMPORTS=(
    fastapi uvicorn jinja2 sqlalchemy asyncpg alembic
    jose bcrypt pydantic pydantic_settings ofxparse
    rapidfuzz multipart httpx
)

for pkg in "${CORE_IMPORTS[@]}"; do
    if python -c "import $pkg" 2>/dev/null; then
        pass "$pkg"
    else
        fail "$pkg — not installed"
        MISSING_PKGS+=("$pkg")
    fi
done

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    warn "Installing missing packages from requirements.txt …"
    pip install -q -r requirements.txt
    pass "pip install completed"
fi

# ── 4. .env file ────────────────────────────────────────────────────
header "Checking configuration"

if [[ -f .env ]]; then
    pass ".env file exists"
else
    warn ".env missing — copying from .env.example"
    if [[ -f .env.example ]]; then
        cp .env.example .env
        pass "Created .env from .env.example (review and edit as needed)"
    else
        fail "No .env or .env.example found"
        ((ERRORS++))
    fi
fi

# ── 5. Docker engine ────────────────────────────────────────────────
header "Checking Docker"

if ! command -v docker &>/dev/null; then
    fail "Docker CLI not found — install Docker Desktop for macOS"
    exit 1
fi

if docker info &>/dev/null; then
    DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    pass "Docker engine running (v$DOCKER_VER)"
else
    fail "Docker engine is not running — start Docker Desktop first"
    exit 1
fi

if ! command -v docker compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    fail "docker compose plugin not available"
    exit 1
fi
pass "docker compose available"

# ── 6. PostgreSQL container ─────────────────────────────────────────
header "Checking PostgreSQL (via Docker Compose)"

DB_STATUS=$(docker compose ps --format '{{.State}}' db 2>/dev/null || echo "missing")

if [[ "$DB_STATUS" == "running" ]]; then
    pass "PostgreSQL container already running"
else
    info "Starting PostgreSQL container …"
    docker compose up db -d
    pass "PostgreSQL container started"
fi

# Wait for healthy
info "Waiting for PostgreSQL to be healthy …"
RETRIES=0
MAX_RETRIES=30
while [[ $RETRIES -lt $MAX_RETRIES ]]; do
    HEALTH=$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q db)" 2>/dev/null || echo "starting")
    if [[ "$HEALTH" == "healthy" ]]; then
        break
    fi
    sleep 1
    ((RETRIES++))
done

if [[ "$HEALTH" == "healthy" ]]; then
    pass "PostgreSQL is healthy"
else
    fail "PostgreSQL did not become healthy after ${MAX_RETRIES}s"
    exit 1
fi

# Quick connectivity test via the pg_isready inside the container
if docker compose exec -T db pg_isready -U finance -d finance_tracker &>/dev/null; then
    pass "Database accepting connections (finance_tracker)"
else
    fail "Cannot connect to finance_tracker database"
    ((ERRORS++))
fi

# ── 7. Port availability ────────────────────────────────────────────
header "Checking port $PORT"

if lsof -i :"$PORT" -sTCP:LISTEN &>/dev/null; then
    PID=$(lsof -ti :"$PORT" -sTCP:LISTEN | head -1)
    warn "Port $PORT already in use (PID $PID)"
    warn "Stop the other process or set PORT=<number> to use a different port"
    ((ERRORS++))
else
    pass "Port $PORT is available"
fi

# ── 8. Alembic migrations ───────────────────────────────────────────
header "Running database migrations"

if alembic upgrade head 2>&1; then
    pass "Alembic migrations applied"
else
    fail "Alembic migrations failed (see output above)"
    ((ERRORS++))
fi

# ── Summary & launch ────────────────────────────────────────────────
if [[ $ERRORS -gt 0 ]]; then
    header "Pre-flight completed with $ERRORS error(s) — review the issues above"
    exit 1
fi

header "All checks passed — launching Finla on http://localhost:$PORT"
echo ""

exec uvicorn app.main:app --reload --port "$PORT"
