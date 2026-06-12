# Finla — Integrity Audit & Build Plan (entry point)

**Reference handle:** `docs/audit/` (point any new session here.)

This folder is the canonical plan for making Finla a secure, multi-user household
finance platform with strong technical + financial integrity. Read these in order:

1. [finla-audit-2026-06.md](finla-audit-2026-06.md) — the full audit: current/target
   state, gap analysis, financial control model, revised data model, ranked backlog,
   acceptance criteria, phased roadmap.
2. [spec-00-security.md](spec-00-security.md) — build-ready spec for compartment #0
   (tenant-isolation IDOR fixes + auth hardening). Decisions locked (§8); two PRs.
3. [spec-01-balance-authority.md](spec-01-balance-authority.md) — build-ready spec for
   compartment #1 (single balance authority, derive-on-read). **Done — merged.**
4. [progress.md](progress.md) — running build log: what's done, what's next, known debt.
   **New session? Read this first to see where to start.**

## Build order (locked)
0. **Security** (independent launch-blocker) — sql_tool isolation, IDORs, login
   rate-limit, cookie `secure`, rotate `.env` secrets. *(/sql route already disabled.)*
1. **Authoritative model + single balance authority** — derive-on-read; one
   `balance(account, basis)` function; drop `current_balance`.
2. **Transaction ingestion integrity** — one dedup rule + DB constraints + override path.
3. **External reconciliation** — strict `reported`/`posted`/`cleared` semantics.
4. **Net worth + reporting outputs** — collapse onto compartment #1.

## Decisions locked (2026-06-11)
- Balances are **derived on read**, not cached.
- Default basis = **`POSTED`** for headline balance and net worth.
- `reported_balance` = **external bank-reported reference** (reconcile against, not authoritative).
- `cleared` is **strict** — owned by finalised reconciliation; `cleared ⊆ posted`.
- **Dedup-override path** admits intentional repeats (auditable); accidental re-imports rejected.

## Status
Compartment **#1 built and merged** ([PR #11](https://github.com/DRCUOA/finance-tracker/pull/11)).
Compartment **#0** in progress — `/sql` route disabled & committed (`1f99b93`); spec
drafted ([spec-00-security.md](spec-00-security.md)); IDOR fixes + auth hardening not yet
built. #2–#4 not started.
See [progress.md](progress.md) for the full log and the next-session starting point.
