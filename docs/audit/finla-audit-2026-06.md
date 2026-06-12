# Finla — Architectural & Financial Integrity Audit

**Date:** 2026-06-11
**Scope:** Full backend + data model review against intended purpose.
**Intended purpose:** A secure, scalable, multi-user household finance platform that strangers can sign up for, built on strong **technical integrity** (sound architecture, single source of truth, DRY, secure access control, reliable data model) and **financial integrity** (accurate, internally-consistent records that reconcile externally to the bank).
**Scale target:** "Small but real" — tens to low-hundreds of users, single Postgres. Correctness & security over horizontal scale.

---

## 1. Current-state assessment

The app is structurally layered (`models/`, `routers/`, `services/`, `schemas/`) and on the surface looks clean. The problem is **semantic, not structural**: the same financial facts are computed and stored in multiple places that drift. This is the "city sprawl" — each feature was built with limited regard to a shared model of truth.

### 1.1 The core defect: duplicated balance truth

`account balance = initial_balance + Σ(transactions)` is **reimplemented 6 times**, each with a *different* filter:

| Site | Filter | Includes pending? |
|---|---|---|
| `services/accounts.py:120` `recalculate_balance` | `is_pending=False` | No |
| `services/reconciliation.py:18` `get_cleared_balance` | `is_cleared=True` | (cleared only) |
| `services/feed_reconciliation.py:163` | `is_pending=False` | No |
| `services/printable_statement.py:296` `_opening_balance` | posted, `date<X` | No |
| `services/reports.py:330` `net_balance_history` | **all rows** | **Yes** |
| `services/migration.py:429` | **all rows** | **Yes** |

Two of the six include pending transactions; four exclude them. The net-worth chart (reports.py) and the headline balance therefore **cannot agree** on any account with pending rows.

### 1.2 Three competing balances on one account

`Account` stores three money fields (`models/account.py:84-98`):
- `initial_balance` — opening seed.
- `current_balance` — a **denormalized cache** of "balance now".
- `reported_balance` — the external bank-reported reference (Akahu feed). A reference to reconcile against, **not** an authoritative override of the ledger.

Cache drift mechanisms:
- **Transaction service never updates the cache.** `create/update/delete_transaction`, `batch_delete`, `batch_categorise` (`services/transactions.py:149-378`) do not touch `current_balance`. Correctness depends on every *router* caller remembering to call `recalculate_balance`. Any non-router caller (API, script, job) silently drifts it.
- **Interest writes by increment, not recompute** (`services/interest.py:244,423`: `current_balance += amount`). This compounds any pre-existing error and is lost/double-applied under concurrency. Interest also *reads* the cache to size the principal (`interest.py:213`), so a wrong cache → wrong interest → wronger cache.
- **Migration seeds the cache including pending** (`migration.py:429`), so the next `recalculate_balance` silently changes it → phantom jump.
- **`reported_balance` is never reconciled into anything.** The gap `reported − posted − pending` is computed display-only (`feed_reconciliation.py:171`); nothing auto-corrects.
- **No row locking.** Concurrent tx sync + manual edit + interest job read-modify-write the cache in separate sessions; last-writer-wins.

### 1.3 Net worth computed two ways

- `routers/dashboard.py:63`, `routers/accounts.py:37` → `sum(current_balance)` (reads cache).
- `services/reports.py:330` → re-sums the ledger.

They agree only if the cache is perfect. The asset/liability roll-up is itself duplicated 3× (dashboard, accounts, reports).

### 1.4 Transaction ingestion: inconsistent dedup

- The fuzzy dedup rule (`reference` first, else `date+amount+lower(trim(description))`) is **copy-pasted 3×**: `import_service._is_duplicate` (304), `transactions.check_duplicate` (111), `import_service.find_duplicates` (335).
- **DB uniqueness is inconsistent by source:** Akahu has a strong partial unique index on `(source, akahu_transaction_id)`; CSV/null-source relies on a reference-only index; **manual transactions have no constraint at all**. The date+amount+desc tier is never DB-enforced for any path.
- **No cross-source dedup:** the same real payment from Akahu *and* CSV/manual both persist → double count.
- **CSV reused-reference silently drops** genuine transactions (the bug fixed for Akahu in migration 019, never fixed for CSV/OFX).
- Re-importing a CSV with blank reference races between SELECT and INSERT → double-count (no DB backstop).

### 1.5 Reporting

Good news: every *analytical* report in `reports.py` is ledger-sourced (sums transactions), not balance-cache-sourced. The outliers are the dashboard/accounts net-worth tiles (§1.3). So once the balance authority and ledger integrity are fixed, reporting largely falls into line.

### 1.6 Security (multi-tenant — gates stranger signup)

| Sev | Location | Issue |
|---|---|---|
| **CRITICAL** | `services/sql_tool.py:95` `_inject_user_filter` | Filter spliced as `WHERE user_id=:user_id AND <user clause>`. SQL `AND` binds tighter than `OR`, so `... WHERE 1=1 OR 1=1` → `(user_id=X AND 1=1) OR 1=1` = all rows. Any logged-in user can **read or DELETE every tenant's transactions**. Verified by reading the regex. |
| **HIGH** | `routers/imports.py:171` `confirm_import` | `account_id`/`statement_id` taken from form with no ownership check; `import_statement_lines` does `db.get(Statement, id)` unscoped → cross-tenant read + write into arbitrary account. |
| **MEDIUM** | `routers/reconciliation.py:79,103` | `save_draft`/`finish` don't verify account ownership before creating recon rows / calling `recalculate_balance`. |
| **HIGH** | `services/auth.py`, `routers/auth.py` | No login rate-limiting / lockout (credential stuffing). Cookie `secure` flag not set (`auth.py:49`). Weak `SECRET_KEY` fallback in `config.py:8`. No password-strength/email validation on signup. |
| **HIGH** | `.env` | Live `SECRET_KEY` + Akahu tokens present in working tree — confirm gitignored and rotate the Akahu credentials. |

**Verified-safe:** accounts, transactions, categories, commitments, budgets, backup, matching_rules, printable_statement, bank_feeds all scope queries by `user_id`.

---

## 2. Target-state definition

**Principle: one fact, one place, derived not stored where feasible.**

- **The transaction ledger is the single source of truth** for all monetary figures. Balances are **derived on read**, not cached. (Decision locked.)
- **One balance function:** `balance(account, *, basis)` where `basis ∈ {posted, cleared, all}`. Every consumer calls it; no module re-sums transactions independently. **`posted` is the default basis** for the headline account balance and net worth (decision locked 2026-06-11).
- **One net-worth function** built on the balance function; dashboard, accounts, and reports all call it.
- **One ingestion identity + dedup model:** a single canonical "transaction identity" and one dedup routine, backed by **DB-level uniqueness constraints** that hold regardless of source.
- **External reconciliation** is an explicit, named model: `reported` (external bank-reported reference) vs `posted` (ledger) vs `cleared` (confirmed present on a finalised statement reconciliation — see §4 for the strict definition), with defined semantics and a defined correction path.
- **Multi-tenant isolation is enforced structurally**, not per-endpoint by hand: every tenant-scoped query goes through a helper/dependency that *requires* `user_id`; the raw-SQL tool is redesigned or removed.
- **Money is `Decimal` end to end** (already true) with one rounding policy.

---

## 3. Gap analysis (current → target)

| Area | Current | Target | Gap |
|---|---|---|---|
| Balance computation | 6 divergent copies + 3-field model + cache drift | 1 derive-on-read function, cache removed | Write function; delete copies; drop `current_balance`; rewrite interest to not depend on cache |
| Pending semantics | Inconsistent (some include, some exclude) | Explicit `basis` enum, one definition | Define enum; audit each call site to pick correct basis |
| Net worth | 2 computations, 3 roll-up copies | 1 function | Collapse onto balance function |
| Dedup | 3 copies, source-dependent DB coverage, no cross-source | 1 rule + DB constraints across sources | Unify rule; add constraints; define cross-source identity; fix CSV reference-drop |
| Reconciliation | reported never reconciled; display-only delta | Defined semantics + correction path | Design the truth model (compartment #4) |
| Tenant isolation | 1 critical hole + 2 IDORs + ad-hoc per endpoint | Structural enforcement | Fix sql_tool; fix IDORs; introduce scoped-query helper |
| Auth hardening | no rate-limit, cookie insecure, weak secret fallback | hardened | Add rate-limit, set secure, enforce strong secret, validate signup |

---

## 4. Financial control model (layered checks & balances)

A transaction flows through five stages; each stage has an integrity control:

1. **Capture** (manual / CSV / OFX / feed) → *Control:* one identity + dedup rule + DB uniqueness. No duplicate or dropped row can enter the ledger — **except** via the explicit dedup-override path (below), which admits a genuine repeat as a distinct, intentionally-recorded row.
2. **Categorisation** → *Control:* category belongs to the same user; uncategorised is an explicit state, not a silent null that breaks reports.
3. **Budgeting** → *Control:* budget figures compared against ledger-derived actuals only (already true).
4. **Reconciliation** → *Control:* `posted` (ledger) reconciled against `reported` (external bank-reported reference) with a defined, auditable delta and correction path; `cleared` is a strict state (see definition below), stored historically.
5. **Reporting / net worth** → *Control:* every output derived from the ledger via the single balance/net-worth functions. No output reads a denormalized cache.

### Definition of `cleared` (strict)

`cleared` is **not** a free-floating user toggle. A transaction is `cleared` **iff** it has been matched to a line on a **finalised statement reconciliation** for its account — i.e. the user confirmed it appears on an external statement that was reconciled to completion. Consequences:
- Setting `is_cleared=True` outside the reconciliation flow is disallowed; the flag is owned by `finish_reconciliation`.
- Un-finalising / deleting a reconciliation must revert the `is_cleared` flag on its matched transactions.
- `cleared` ⊆ `posted` (a pending transaction can never be cleared, since it isn't yet on a settled statement).
- A `cleared`-basis balance is therefore "the balance the bank statement agreed with", distinct from `posted` ("everything settled in our ledger") and `reported` ("the live bank-reported reference").

### Dedup-override path (legitimate repeats)

Some genuine transactions are byte-identical to another on the same day (two $4.50 coffees, two identical transfers). The dedup rule would wrongly drop the second. The override path:
- A user-initiated `allow_duplicate` intent (the existing `force=True` on manual create, generalised) admits the row **and records that the override was used** (e.g. an `dedup_override` marker / audit note on the transaction), so it is auditable rather than silent.
- Override is only available on user-driven paths (manual entry, CSV import confirmation UI). Automated feed sync (Akahu) never overrides — its identity is a stable external id, so true repeats already carry distinct ids.
- The DB uniqueness constraint must accommodate an admitted repeat (e.g. the `dedup_key` incorporates an occurrence/sequence discriminator when an override is applied), so the constraint still blocks *accidental* re-imports while permitting *intentional* repeats.

**Invariant (the definition of "correct"):** for any account and any point in time,
`balance(account, basis=all) == initial_balance + Σ(transaction.amount for that account up to that time)`.
This must hold by construction (derive-on-read makes it a tautology), and `reported_balance` is reconciled against `balance(account, basis=posted)` with the residual surfaced and explainable.

---

## 5. Technical recommendations

- **Derive-on-read balances.** Remove `Account.current_balance`. Introduce `app/services/balances.py` with `balance(session, account, *, basis, as_of=None)` and `net_worth(session, user_id, *, as_of=None)`. (Compartment #1.)
- **Define `BalanceBasis` enum** (`posted`, `cleared`, `all`) co-located with the function; document what each means.
- **Single dedup module** `app/services/dedup.py` with one `transaction_identity(tx)` and one `is_duplicate(...)`; back it with DB constraints; define cross-source identity. (Compartment #2.)
- **Scoped-query helper / dependency** so isolation is structural. Redesign the raw-SQL tool to parametrise properly (or gate it behind a role / remove it). (Compartment #0.)
- **Interest job** must stop incrementing a cache; it should *insert interest transactions* into the ledger (it already does at `interest.py:244` context — verify and make the transaction the only effect).
- **Concurrency:** with derive-on-read the read-modify-write races on the cache disappear; remaining writes (interest tx insert, feed sync) are plain inserts/upserts guarded by DB uniqueness.
- **Keep `Decimal(14,2)`**; centralise rounding.
- **Tests:** each compartment ships with a property test asserting the §4 invariant.

---

## 6. Revised data model

`Account` (changes):
- **Remove** `current_balance` (derived on read).
- **Keep** `initial_balance` (opening seed), `reported_balance` + `reported_balance_as_of` (external bank-reported reference, input to reconciliation), `last_synced_at`, `transactions_as_of`.
- Interest config fields unchanged.

`Transaction` (changes):
- Add a canonical identity supporting cross-source dedup. Proposed: a generated `dedup_key` (e.g. hash of `account_id|date|amount|normalised_description` when no stable external id) **plus** the existing `(source, akahu_transaction_id)`. When the dedup-override path admits an intentional repeat, the `dedup_key` incorporates an occurrence/sequence discriminator so the row is distinct under the constraint.
- Add a `dedup_override` marker (bool/note) recording that a row was admitted via the override path — keeps legitimate repeats auditable rather than silent.
- DB constraints:
  - `unique (source, akahu_transaction_id) where both not null` (exists — keep).
  - `unique (account_id, dedup_key)` covering **all** sources including manual (new). The dedup-override path keeps the constraint intact by giving an intentional repeat a distinct `dedup_key` (occurrence discriminator) — so accidental re-imports are still blocked while genuine repeats are admitted.
- Clarify `is_pending`, `is_cleared`, `is_source_stale` as the basis dimensions.

`Reconciliation` (compartment #4): formalise the stored statement reconciliation; define how/whether a confirmed reconciliation adjusts the ledger (e.g. via an explicit adjustment transaction rather than a balance edit).

*(Each migration is small and reversible; sequenced per compartment.)*

---

## 7. Prioritised build backlog

| # | Compartment | Outcome | Risk |
|---|---|---|---|
| **0** | **Security hardening** (independent, launch-blocker) | sql_tool isolation fixed/removed; imports & reconciliation IDORs closed; login rate-limit; cookie `secure`; strong `SECRET_KEY` enforced; `.env` secrets rotated & confirmed gitignored | High urgency, low architectural coupling |
| **1** | **Authoritative model + single balance authority** | `current_balance` removed; `balance()`/`net_worth()` functions; 6 copies deleted; interest no longer cache-dependent; `BalanceBasis` defined | Touches many call sites; well-bounded |
| **2** | **Transaction ingestion integrity** | one dedup rule; cross-source identity; DB uniqueness across all sources; CSV reference-drop fixed | Migration + backfill of dedup keys |
| **3** | **External reconciliation truth model** | defined reported/posted/cleared semantics; auditable delta; correction path | Design-heavy |
| **4** | **Net worth + reporting outputs** | dashboard/accounts/reports all call `net_worth()`; remove roll-up duplication | Low risk once #1 done |

Ordering rationale (user's framing): *establish the authoritative model → clean & validate ingestion → reconcile externally → derive outputs last.* Security runs as a parallel first track because it is independent of the architecture work and gates signup.

---

## 8. Acceptance criteria (definition of done per compartment)

**#0 Security**
- A logged-in user provably cannot read or modify another user's data via the SQL tool (regression test with an `OR 1=1` payload returns only own rows / is rejected).
- `confirm_import` and reconciliation `save_draft`/`finish` reject foreign `account_id`/`statement_id` (403).
- Login is rate-limited / locks out after N failures (test).
- Cookies set `secure=True`; app refuses to boot with the default `SECRET_KEY`.
- `.env` confirmed gitignored; Akahu tokens rotated.

**#1 Balance authority**
- `grep` finds exactly one implementation of `initial_balance + Σ(transactions)`.
- `Account.current_balance` column removed; no reference remains.
- Property test: for random ledgers, `balance(basis=all) == initial_balance + Σ(amount)` holds; `posted`/`cleared` honour their filters.
- Dashboard, accounts, reports net-worth values are byte-identical (same function).
- Interest accrual produces a ledger transaction and no separate cache write.

**#2 Ingestion integrity**
- Exactly one dedup routine; the 3 copies are gone.
- DB rejects a duplicate across *every* source (test per source incl. manual).
- Re-importing the same CSV twice adds zero rows; a CSV with a reused reference but distinct payment adds both.
- No cross-source double count for the same real payment (defined identity test).
- **Dedup-override path:** a user can intentionally admit a genuine repeat (e.g. two identical same-day purchases); the row is stored as distinct, marked `dedup_override`, and remains auditable. Accidental re-import (no override) is still rejected. Override is unavailable on automated feed sync. (Test both: override admits; absence rejects.)

**#3 Reconciliation**
- `reported` (external bank-reported reference), `posted`, `cleared` have documented definitions and a single delta computation.
- **`cleared` is strict:** `is_cleared` is owned by `finish_reconciliation`; it cannot be set outside the reconciliation flow; un-finalising a reconciliation reverts it; `cleared ⊆ posted`. (Test: a pending txn can never be cleared; deleting a reconciliation clears the flag on its matched rows.)
- A confirmed reconciliation's effect on the ledger is explicit and auditable (no silent balance edit).

**#4 Outputs**
- All net-worth/roll-up code paths call the single `net_worth()`; duplication removed.
- Reports and dashboard agree on every shared figure.

---

## 9. Phased roadmap

- **Phase A — Make it safe (compartment #0).** Can ship independently and *before* any stranger signup. ~small, urgent.
- **Phase B — Make it true (compartments #1 → #2).** Establish the derive-on-read model, then clean ingestion into it. The bulk of the architecture work; each is one PR with its migration and property tests.
- **Phase C — Make it reconcilable (compartment #3).** Design + implement the external reconciliation truth model.
- **Phase D — Make it visible (compartment #4).** Collapse all outputs onto the single functions; verify cross-screen agreement.

Each phase ends with the §8 acceptance criteria met and the §4 invariant property-tested. No compartment starts until the previous one's criteria pass — this is how we prevent the sprawl from regrowing.
