# Finla — Audit Build Progress

**Purpose:** the running "where are we / where to start next" log for the audit
build (see [README.md](README.md) for the plan and locked decisions). Newest
entry first. Update this at the end of every working session.

---

## Snapshot (as of 2026-06-11)

| Compartment | State | Notes |
|---|---|---|
| #0 Security | **In progress (partial)** | `/sql` route disabled only. IDORs, login rate-limit, cookie `secure`, secret rotation still **not started**. |
| #1 Balance authority | **Done — merged** | [PR #11](https://github.com/DRCUOA/finance-tracker/pull/11), merged to `main`. |
| #2 Ingestion integrity | Not started | Spec not yet written. |
| #3 External reconciliation | Not started | One pre-existing test failure already lives here (see below). |
| #4 Net worth + reporting | Partially absorbed by #1 | Net-worth roll-up already unified behind `aggregate_net_worth()`; full consolidation still pending. |

---

## Session log

### 2026-06-11 — Compartment #1 built & merged; #0 /sql disable started

**Compartment #1 (Authoritative Model + Single Balance Authority) — COMPLETE.**
Shipped in [PR #11](https://github.com/DRCUOA/finance-tracker/pull/11) (merged to `main`).

What landed:
- **`app/services/balances.py`** is the single implementation of
  `initial_balance + Σ(transactions)`: `balance()`, `balances_for()`,
  `aggregate_net_worth()`, `net_worth()`. One `basis` filter
  (`ALL`/`POSTED`/`CLEARED`, default `POSTED`); inclusive `as_of`.
- **`accounts.current_balance` cache removed.** Migration
  `020_drop_account_current_balance.py` drops the column (backfilling
  `downgrade`). `recalculate_balance()` deleted along with all call sites.
- **6 divergent balance re-implementations collapsed** onto `balances.py`:
  interest, reconciliation, feed reconciliation, printable statement, reports,
  akahu sync.
- **Net-worth roll-up unified** behind `aggregate_net_worth()` — dashboard,
  accounts, reports all use it. Caller scope intentionally preserved (no
  visible number change), per the locked "preserve current scope" decision.
- **`tests/test_balances.py`** added (derive-on-read property test, basis
  filters, inclusive `as_of` boundary, tenant isolation, cross-screen
  agreement, interest single-effect). Existing tests rewritten off the cache.

Behaviour changes (deliberate, decided 2026-06-11):
- Reports net-balance chart switched from `ALL` to `POSTED` basis.
- Default basis for headline balance and net worth is `POSTED`.

One real bug fixed in passing: interest first-run seeding broke because the new
`balance()` query autoflushed a freshly-added account and populated its
server-default `created_at`, hiding the first-run state. Fixed by capturing the
baseline timestamp **before** the balance query
(`app/services/interest.py`, `accrue_interest_for_account`).

**Compartment #0 (Security) — /sql disable started (NOT yet committed/PR'd).**
- `/sql` route disabled: `app/main.py` (router include commented out) and
  `app/templates/base.html` (nav item removed). These are **uncommitted on
  `main`** — they were deliberately kept out of PR #11 to honour the
  compartmentalised-spec working style.
- Fixed a Jinja crash introduced by the nav edit: a `{# … #}` comment had been
  placed *inside* the `{% set nav_items = [ … ] %}` list literal (invalid —
  Jinja parses an expression there, `TemplateSyntaxError` on every page that
  extends `base.html`). Moved the note to a proper comment above the `{% set %}`
  block. `base.html` + `auth/login.html` now parse.

---

## Where the next session should start

1. **Commit the compartment-#0 /sql disable.** `app/main.py` +
   `app/templates/base.html` are uncommitted on `main`. Either commit them
   directly (small, self-contained security hardening) or open a compartment-#0
   PR. Also commit/track the `docs/audit/` folder itself (currently untracked).
2. **Run the migration on the dev DB:** `alembic upgrade head` (brings the
   Postgres dev schema to `020`, dropping `current_balance`). Tests use SQLite
   built from the models, so they don't exercise the Alembic path — verify it
   manually.
3. **Continue compartment #0 (the launch-blocker):** remaining items are IDOR
   sweeps, login rate-limiting, cookie `secure` flag, and rotating `.env`
   secrets. None started.
4. **Then compartment #2 (ingestion integrity):** needs a spec first
   (`spec-02-*.md`), mirroring the structure of
   [spec-01-balance-authority.md](spec-01-balance-authority.md).

## Known issues / debt

- **Pre-existing test failure (compartment #3):**
  `tests/test_feed_reconciliation.py::TestSyncAccountBalancesFeedFields::test_captures_refreshed_balance_timestamp`
  fails on a tz-naive vs tz-aware `reported_balance_as_of` comparison under
  SQLite. It fails on `main` independently of #1 work — it belongs to the
  external-reconciliation compartment. Full suite is otherwise green
  (147 passed, 1 failed).
- **`app/services/migration.py:130`** still contains the string
  `current_balance`, but that reads a key from an **external import-data dict**
  (legacy import preview), not the removed ORM column — intentionally left.
