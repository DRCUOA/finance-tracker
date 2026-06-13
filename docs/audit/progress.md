# Finla — Audit Build Progress

**Purpose:** the running "where are we / where to start next" log for the audit
build (see [README.md](README.md) for the plan and locked decisions). Newest
entry first. Update this at the end of every working session.

---

## Snapshot (as of 2026-06-12)

| Compartment | State | Notes |
|---|---|---|
| #0 Security | **Code complete — merged** | PR A (tenant isolation) [#12](https://github.com/DRCUOA/finance-tracker/pull/12) **merged**; PR B (auth hardening) [#13](https://github.com/DRCUOA/finance-tracker/pull/13) **merged**. Remaining: rotate the live `.env` secrets (operational) — `.env` is already gitignored & untracked. |
| #1 Balance authority | **Done — merged** | [PR #11](https://github.com/DRCUOA/finance-tracker/pull/11), merged to `main`. |
| #2 Ingestion integrity | **Built — awaiting PR/merge** | One `dedup` module + cross-source `content_hash` identity, migration `022` (applied live), all call sites unified. Branch `spec-02-ingestion-integrity`. |
| #3 External reconciliation | Not started | One pre-existing test failure already lives here (see below). |
| #4 Net worth + reporting | Partially absorbed by #1 | Net-worth roll-up already unified behind `aggregate_net_worth()`; full consolidation still pending. |

---

## Session log

### 2026-06-13 — Compartment #2 built (ingestion integrity)

Branch `spec-02-ingestion-integrity` (off `main`). Implements spec-02 as **one PR**.

- **One dedup module** — `app/services/dedup.py` is the single source of identity:
  `content_hash` = sha256 of `date | amount(signed, 2dp) | normalised description`
  (account-independent); `normalise_description`; `is_duplicate`; `next_occurrence`;
  and `admit(...)`, the one entry point every insert path calls. The 3 copy-pasted
  rules (`import_service._is_duplicate`/`find_duplicates`,
  `transactions.check_duplicate`) are **deleted** — `grep -rn` for the old names in
  `app/` now hits only this module's docstring.
- **Cross-source DB identity** — `Transaction` gains `content_hash` /`occurrence`
  /`dedup_override` and `unique(account_id, content_hash, occurrence)`. Migration
  `022` adds the columns, **backfills in Python through `dedup`** (auto-discriminate:
  ascending `occurrence` per content group, `dedup_override` on repeats, nothing
  deleted), sets `content_hash NOT NULL`, creates the constraint, and **drops** the
  old reference-uniqueness index (the cause of the CSV reused-reference silent-drop).
  Applied live to the dev DB (2876 rows, 0 nulls, 0 pre-existing dupes).
- **Call sites unified** — `create_transaction` routes through `admit(override=force)`;
  `update_transaction` recomputes `content_hash` + re-checks on content/account change;
  `import_statement_lines` + `find_duplicates` go through `dedup`; Akahu sync computes
  `content_hash`/`occurrence=0`, **never overrides**, and **adopts** a content-matching
  non-feed row (writes `source`/`akahu_*` onto it) instead of double-counting. Router
  edit paths catch `DuplicateTransactionError` (409 / `?error=duplicate`) with the
  constraint as backstop; messages no longer mention "reference".

Tests: `tests/test_transactions_dedupe.py` rewritten (18) for the content model —
idempotent re-import, reused-ref/distinct-payment both insert, override→`occurrence 1`
+`dedup_override`, concurrency backstop (DB constraint), feed adoption (incl. live
`sync_account_transactions`), backfill algorithm + hash-drift guard, single-rule grep
guard, and route 409/redirect handling. Fixed one `test_akahu` fixture that predated
`content_hash`. Full suite **177 passed, 1 known pre-existing fail**
(`test_feed_reconciliation` tz-naive/aware — compartment-#3 debt, not a regression).

Next: rotate the live `.env` secrets (remaining compartment-#0 item); then spec
compartment #3 (external reconciliation).

### 2026-06-13 — Compartment #2 spec drafted (ingestion integrity)

[spec-02-ingestion-integrity.md](spec-02-ingestion-integrity.md) — build-ready,
grounded in the actual code (the 3 copy-pasted dedup rules, the source-dependent
DB indexes, the Akahu upsert, the CSV reused-reference drop). Scopes: a single
`app/services/dedup.py`, a cross-source **content identity** (`content_hash` =
account+date+amount+normalised description), DB uniqueness across **all** sources
incl. manual (`unique(account_id, content_hash, occurrence)`), the CSV/OFX
reference-drop fix, and an audited override path for genuine repeats.

**§8 decisions LOCKED 2026-06-13:** (1) **content key, all sources** — identity is
content, not `reference`; Akahu keeps its `(source, akahu_transaction_id)`
constraint and gets an adoption rule for exact cross-source matches; (2) backfill
**auto-discriminates & keeps all** existing duplicates (ascending `occurrence`,
flagged `dedup_override`; nothing deleted); (3) override = **occurrence
discriminator + `dedup_override` marker** (the existing manual `force=True`
generalises in); (4) ships as **one PR** (module + call-site unification +
migration `022`/backfill/constraints).

Next: build compartment #2 per the spec; then rotate the live `.env` secrets
(the one remaining compartment-#0 item).

### 2026-06-12 — Compartment #0 PR B (auth hardening) built

Branch `spec-00-auth-hardening` (cut from `main` before PR A merged; PR A's
tenant code was later merged in — see the merge note at the end of this entry).
Implements the three §8 auth launch-blockers:

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

Merged as [PR #13](https://github.com/DRCUOA/finance-tracker/pull/13). PR A
([#12](https://github.com/DRCUOA/finance-tracker/pull/12)) merged to `main` first,
so `main` was merged back into this branch before merge — only `progress.md`
conflicted (both PRs prepended a log entry; kept both). `app/main.py` auto-merged
(PR A's sql_tool removal + PR B's `validate_security_config()` boot check coexist).
Post-merge suite: **170 passed, 1 known pre-existing fail**.

With PR A + PR B merged, **compartment #0 is code-complete**. Next: rotate live
`.env` secrets (operational); then spec compartment #2 (ingestion integrity).

### 2026-06-12 — Compartment #0 PR A built (tenant isolation)

Branch `spec-00-tenant-isolation` (from [spec-00-security.md](spec-00-security.md) PR A).

- **sql_tool deleted entirely** — `app/routers/sql_tool.py`, `app/services/sql_tool.py`,
  `app/templates/sql_tool/` removed; the commented-out include + import cleaned out of
  `app/main.py`. `grep -r sql_tool app/` is empty.
- **`app/dependencies.py` added** — `require_owned_account` (path-param dependency,
  404 on foreign) + `get_owned_account_or_404` (imperative helper for form-sourced
  account ids). **404, not 403** (locked §8.2).
- **Reconciliation IDOR closed** — `save_draft`, `finish_reconciliation`,
  `discard_draft` now depend on `require_owned_account`, so a forged `account_id`
  404s before any `Reconciliation` row is created/overwritten. (The service
  previously created/overwrote rows with a foreign `account_id` — verified.)
- **Imports IDOR closed** — `upload_file`, `map_csv_fields`, `map_ofx_fields`,
  `confirm_import` validate account ownership; `confirm_import` also validates
  statement ownership. Service `import_statement_lines` hardened as defence-in-depth:
  loads the statement scoped to `user_id` and skips any line whose `statement_id`
  doesn't match (stops cross-tenant line-content exfiltration into the caller's ledger
  and the foreign `Statement.status` flip).
- **`tests/test_tenant_isolation.py`** — 10 tests: sql_tool routes 404; reconciliation
  save_draft/finish foreign → 404 + no write, owner allowed; confirm_import foreign
  account/statement → 404 + no write; upload foreign → 404; service-level statement +
  line scoping and owner happy-path. Endpoint tests drive the real app via
  `httpx.ASGITransport` with `get_db`/`require_user` overridden (no lifespan/scheduler).
- **Suite:** 157 passed, 1 failed — the failure is the **known pre-existing**
  compartment-#3 `feed_reconciliation` tz-naive/aware test (unrelated).

**Next:** open PR A, then build PR B (auth hardening: SECRET_KEY boot check, cookie
`COOKIE_SECURE`, login rate-limit).

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
