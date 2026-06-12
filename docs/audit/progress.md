# Finla — Audit Build Progress

**Purpose:** the running "where are we / where to start next" log for the audit
build (see [README.md](README.md) for the plan and locked decisions). Newest
entry first. Update this at the end of every working session.

---

## Snapshot (as of 2026-06-12)

| Compartment | State | Notes |
|---|---|---|
| #0 Security | **In progress** | PR A (tenant isolation) + PR B (auth hardening) built on branches. Remaining: merge both PRs; rotate the live `.env` secrets (operational). |
| #1 Balance authority | **Done — merged** | [PR #11](https://github.com/DRCUOA/finance-tracker/pull/11), merged to `main`. |
| #2 Ingestion integrity | Not started | Spec not yet written. |
| #3 External reconciliation | Not started | One pre-existing test failure already lives here (see below). |
| #4 Net worth + reporting | Partially absorbed by #1 | Net-worth roll-up already unified behind `aggregate_net_worth()`; full consolidation still pending. |

---

## Session log

### 2026-06-12 — Compartment #0 PR B (auth hardening) built

Branch `spec-00-auth-hardening` (off `main`; does **not** include PR A's tenant
code). Implements the three §8 auth launch-blockers:

- **Refuse-default-SECRET_KEY boot check** — `app/config.py` gains
  `DEFAULT_SECRET_KEY`, `MIN_SECRET_KEY_LENGTH` (32) and
  `validate_security_config()`, called at the **top of the app lifespan** (boot,
  not import — so tooling/tests that merely import settings aren't forced to
  supply a key). Refuses to start on the shipped placeholder or any <32-char key.
- **Cookie `Secure` flag** — new `COOKIE_SECURE` setting (default **True**;
  set `COOKIE_SECURE=false` for local http dev). `_set_auth_cookies` now passes
  `secure=settings.COOKIE_SECURE` on both the access and refresh cookies.
- **Login rate-limiting** — DB-backed, **5 failures / 15-min window, keyed by the
  (email, ip) pair**. New `LoginAttempt` model + hand-written migration `021`
  (additive `login_attempts` table; **applied live to the dev DB**, 020→021).
  `app/services/auth.py` gains `is_login_locked` / `record_failed_login` /
  `reset_login_attempts`; `POST /login` returns a **generic 429** when locked (no
  user enumeration), records each failure, and clears the counter on success.
  Lock is IP-scoped so an attacker on one host can't lock out the real user.

Tests: `tests/test_auth_hardening.py` (13 — SECRET_KEY validation, cookie flag,
rate-limit service incl. window-expiry + IP-scoping, and two ASGI login-endpoint
lockout/reset tests). All pass. Full suite **160 passed, 1 known pre-existing
fail** (`test_feed_reconciliation` tz-naive/aware — compartment-#3 debt, fails on
`main` independently; not a regression).

Next: open PR B; then merge PR A + PR B; then rotate live `.env` secrets.

### 2026-06-12 — Compartment #0 /sql disable committed; migration confirmed applied

- **`/sql` disable committed** (`1f99b93`): the uncommitted `app/main.py` +
  `app/templates/base.html` edits from the prior session are now on `main` as a
  self-contained compartment-#0 security commit. Verified the only remaining
  `sql_tool` references are inside the now-orphaned router/template (no longer
  reachable — both the import and `include_router` are removed).
- **`docs/audit/` brought under version control** (`dcfe335`) — the folder was
  previously untracked.
- **Migration `020` confirmed already applied** to the dev DB. `alembic current`
  reports `020 (head)`; a direct `information_schema` check confirms
  `accounts.current_balance` no longer exists. No `alembic upgrade` was needed.

Progress-doc steps 1 (commit) and 2 (migration) are now both complete.

**Compartment #0 spec drafted** —
[spec-00-security.md](spec-00-security.md). Scopes the remaining launch-blockers:
imports & reconciliation IDOR fixes (grounded in the actual unscoped `db.get` /
form-`account_id` sites), a `require_owned_account` ownership dependency, cookie
`secure` toggle, refuse-default-`SECRET_KEY` boot check, and login rate-limiting.
Notes that `.env` is **already gitignored & untracked** (so only token *rotation*
remains, an operational step).

**§8 decisions LOCKED 2026-06-12:** sql_tool → **delete entirely**; foreign access
→ **404** (not 403); rate-limit → **5 failures / 15-min window, email+IP, DB-backed**;
cookie → **`COOKIE_SECURE` env, default True / dev False**; signup validation →
**deferred** to a follow-up; **two PRs** — PR A tenant isolation (delete sql_tool +
imports & reconciliation IDORs + `require_owned_account`), PR B auth hardening
(SECRET_KEY boot check + cookie + rate-limit). **Spec is ready to build.**

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

1. **Build compartment #0 PR A (tenant isolation)** from
   [spec-00-security.md](spec-00-security.md): delete sql_tool entirely, add the
   `require_owned_account` dependency, close the reconciliation IDOR
   (`save_draft`/`finish`) and the imports IDOR (`confirm_import`/`map_ofx_fields`
   + scope `import_statement_lines`'s `db.get`). Foreign access → 404. Two-user
   regression test.
2. **Then PR B (auth hardening):** SECRET_KEY boot check → cookie `COOKIE_SECURE`
   toggle → login rate-limit (5/15-min, email+IP, DB table + alembic).
3. **Operational:** rotate Akahu tokens + `SECRET_KEY`.
4. **Then compartment #2 (ingestion integrity):** needs a spec first
   (`spec-02-*.md`), mirroring [spec-01-balance-authority.md](spec-01-balance-authority.md).

_Done 2026-06-12: /sql disable committed (`1f99b93`), docs tracked (`dcfe335`),
migration `020` confirmed applied, [spec-00-security.md](spec-00-security.md)
drafted + decisions locked._

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
