# Spec — Compartment #0: Security Hardening (launch-blocker)

**Status:** Ready to build — key decisions locked 2026-06-12 (see §8). **Depends on:** nothing (independent of the architecture work; gates stranger signup).
**Goal:** Close the multi-tenant isolation holes and harden authentication so the app is safe to expose to strangers. This is the launch-blocker track from the audit (§1.6, §8 "#0 Security").

**Definition of "correct" (the invariant this compartment guarantees):**
A logged-in user can **only ever** read or modify rows belonging to their own `user_id`. Every tenant-scoped mutation validates ownership of the referenced `account_id` / `statement_id` *before* touching data, and the app refuses to run in an insecure configuration.

---

## 0. Scope summary (audit §1.6)

| # | Item | Sev | State entering this compartment |
|---|---|---|---|
| 0.1 | `/sql` raw-query tool cross-tenant hole | CRITICAL | **Route disabled** (`1f99b93`). Permanent fix = **delete** router/template/service (locked §8). |
| 0.2 | `imports` IDOR — unscoped `account_id`/`statement_id` | HIGH | Not started |
| 0.3 | `reconciliation` IDOR — `save_draft`/`finish` no ownership check | MEDIUM | Not started |
| 0.4 | Cookie `secure` flag unset | HIGH | Not started |
| 0.5 | Weak `SECRET_KEY` fallback (boots with default) | HIGH | Not started |
| 0.6 | No login rate-limiting / lockout | HIGH | Not started |
| 0.7 | `.env` secrets — gitignore + rotate Akahu/SECRET_KEY | HIGH | `.env` **already gitignored & untracked**; rotation outstanding (operational) |
| 0.8 | Signup: password-strength / email validation | LOW | Not started (optional this pass) |

**Build order (two PRs, locked §8):**
- **PR A — tenant isolation:** 0.1 (delete sql_tool) + 0.3 (reconciliation IDOR) + 0.2 (imports IDOR, via `require_owned_account`).
- **PR B — auth hardening:** 0.5 (SECRET_KEY boot check) → 0.4 (cookie `secure`) → 0.6 (rate-limit).
- 0.7 (secret rotation) operational; 0.8 (signup validation) deferred to a follow-up.

---

## 1. Tenant isolation — structural enforcement

The audit target (§2) is: *"every tenant-scoped query goes through a helper/dependency that requires `user_id`."* We already have `accounts.get_account(db, account_id, user_id)` returning `None` for foreign/missing accounts, and the reconciliation **GET** already uses it (`reconciliation.py:55-56` → 404). The IDORs are the *mutation* paths that skipped that guard.

**Introduce one ownership dependency** so the 403/404 path is uniform, not re-hand-rolled per endpoint:

```python
# app/routers/deps.py (or app/dependencies.py)
async def require_owned_account(
    account_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Account:
    acct = await accounts.get_account(db, account_id, user.id)
    if acct is None:
        raise HTTPException(status_code=404)   # 404 (not 403) — don't leak existence (locked §8.2)
    return acct
```

For form-sourced `account_id`/`statement_id` (imports), the value isn't a path param, so validate explicitly inside the handler using the same `get_account` (and an equivalent owned-statement lookup) and return 404 on miss.

### 0.2 imports IDOR (`routers/imports.py`)
- `confirm_import` (`@router.post("/confirm")`): `account_id` + `statement_id` come from the form unchecked. **Validate both** belong to `user.id` before calling `import_statement_lines`; 404 on miss.
- `map_ofx_fields`: same — `account_id` from form is unchecked before `create_statement`.
- `services/import_service.py:import_statement_lines`: `db.get(StatementLine, lid)` and `db.get(Statement, statement_id)` are **unscoped**. Scope them: require the statement's `user_id == user_id` (and that each line belongs to that statement); skip/reject otherwise. This is defence-in-depth behind the router check — fix both layers.

### 0.3 reconciliation IDOR (`routers/reconciliation.py`)
- `save_draft` (`/{account_id}/save-draft`) and `finish_reconciliation` (`/{account_id}/finish`) mutate without verifying ownership. Add `account: Account = Depends(require_owned_account)` (or an inline `get_account` guard) so a foreign `account_id` 404s before any recon row is written or `finish` runs.

### 0.1 sql_tool (CRITICAL — DELETE, locked §8.1)
- Route is already **disabled** (import + `include_router` removed; nav item dropped). PR A completes the disposition by **deleting** `app/routers/sql_tool.py`, `app/services/sql_tool.py`, the `app/templates/sql_tool/` templates, and the now-dead commented-out lines in `app/main.py`. Confirm nothing else imports them (`grep -r sql_tool app/`). Reintroduce later behind a proper read-only scoped API only if a real need appears.

---

## 2. Auth hardening (`services/auth.py`, `routers/auth.py`, `config.py`)

### 0.5 Refuse insecure `SECRET_KEY` (smallest, do first)
- `config.py:8` ships `SECRET_KEY = "change-me-to-a-long-random-string"`. Add a startup check that **raises** if `SECRET_KEY` is the default (or shorter than, say, 32 chars). App must not boot insecurely. Tests/dev set a real value via `.env`.

### 0.4 Cookie `secure` flag
- `routers/auth.py:49-50` set `access_token`/`refresh_token` with `httponly=True, samesite="lax"` but **no `secure`**. Add `secure=...` driven by a new `COOKIE_SECURE: bool` setting (default `True`; dev over http://localhost sets it `False` in `.env`). Avoids hard-coding `True` (would break local http) while defaulting safe.

### 0.6 Login rate-limiting / lockout (locked §8.3)
- No throttling today → credential stuffing. Add a **DB-backed** failed-attempt counter keyed by **email + IP**, locking out after **5** failures within a **15-minute** window. New table (e.g. `login_attempts`) via alembic. Return a **generic** error (no user-enumeration) while locked. **Reset on successful login.**

### 0.8 Signup validation — DEFERRED (locked §8.5)
- Minimal password-strength + email-format validation on signup is **out of the compartment-#0 PRs**; tracked as a follow-up to keep scope tight.

---

## 3. Secrets (`0.7` — operational, not code)

- `.env` is **already gitignored and untracked** (verified) — the "confirm gitignored" half is **done**.
- **Outstanding (user action):** rotate the live Akahu `AKAHU_APP_TOKEN` / `AKAHU_USER_TOKEN` and `SECRET_KEY` that have existed in the working tree, since they may have been exposed. This is an operational step for the user; the spec just tracks it. Rotating `SECRET_KEY` invalidates existing sessions (acceptable).

---

## 4. Risks & mitigations

- **404 vs 403:** **404** for foreign resources so we don't leak existence of other tenants' accounts/statements (locked §8.2; supersedes audit §8 "403").
- **Cookie `secure` breaks local dev:** mitigated by the `COOKIE_SECURE` env toggle (default True, dev False).
- **Lockout as a DoS vector:** an attacker could lock a victim by spamming their email. Mitigate by also/instead keying on IP, and keeping the window short. Confirm strategy at §8.
- **Boot-time secret check breaking CI/tests:** ensure the test config and `.env.example` provide a valid non-default secret.
- **Scope creep:** signup validation (0.8) is optional; drop it from this PR if it grows.

---

## 5. Test plan (ships with the PR)

- **sql_tool:** code deleted — `/sql` and `/sql/execute` return 404 (unreachable) and `grep -r sql_tool app/` is empty.
- **imports IDOR:** `confirm_import` / `map_ofx_fields` with a foreign `account_id` or `statement_id` → 404, no rows written into the foreign account.
- **reconciliation IDOR:** `save_draft` / `finish` with a foreign `account_id` → 404, no recon rows created.
- **Cookie:** login response sets `Secure` on both cookies when `COOKIE_SECURE=True`.
- **SECRET_KEY:** app/factory raises when `SECRET_KEY` is the default.
- **Rate-limit:** N failed logins → lockout (subsequent attempt rejected even with correct password during the window); success resets the counter.
- **Cross-tenant regression:** a two-user fixture proves user B cannot read/write user A's accounts, statements, or reconciliations through any of the touched endpoints.

---

## 6. Acceptance criteria (done = all true) — from audit §8

1. A logged-in user provably cannot read or modify another user's data via the SQL tool (route disabled, regression test) — and the permanent disposition (delete/redesign) is decided and applied.
2. `confirm_import` and reconciliation `save_draft`/`finish` reject foreign `account_id`/`statement_id` (404).
3. Login is rate-limited / locks out after N failures (test).
4. Cookies set `secure=True` in production config; app refuses to boot with the default `SECRET_KEY`.
5. `.env` confirmed gitignored (done); Akahu tokens + `SECRET_KEY` rotated (operational sign-off).

---

## 7. Out of scope (later compartments)

- Ingestion identity / dedup constraints → compartment #2.
- `reported_balance` reconciliation semantics → compartment #3.
- Any balance/net-worth change → already shipped in compartment #1.
- Broader authz model (roles/permissions) — not needed at "small but real"; single-tenant ownership is the model.

---

## 8. Decisions — LOCKED 2026-06-12

1. **sql_tool disposition:** **DELETE entirely** — remove the router, template(s), and `services/sql_tool.py`. (Route is already unwired; this removes the dead code.) Reintroduce later behind a safe scoped API only if a real need appears.
2. **Foreign-resource access → `404`** (not 403): treat another tenant's account/statement as non-existent so we don't leak existence. Supersedes the audit §8 "403" wording.
3. **Rate-limit policy:** lock out after **5** failed logins within a **15-minute** window, keyed by **email + IP**, counter **DB-backed** (survives restarts). Generic error (no user enumeration); reset on success.
4. **Cookie toggle:** introduce `COOKIE_SECURE` env setting, default **`True`**, dev sets **`False`** in `.env`.
5. **Signup validation (0.8):** **deferred to a follow-up** — keep it out of the compartment-#0 PRs to avoid widening scope.
6. **PR shape — TWO PRs:**
   - **PR A — tenant isolation:** delete sql_tool (0.1), `require_owned_account` dependency + imports IDOR (0.2) + reconciliation IDOR (0.3).
   - **PR B — auth hardening:** SECRET_KEY boot check (0.5), cookie `secure` toggle (0.4), login rate-limiting (0.6).
   - Secret rotation (0.7) is operational, tracked separately.
