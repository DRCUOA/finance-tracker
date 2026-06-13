# Spec — Compartment #2b: Bank Feed Tenancy Isolation

**Status:** Build immediately (containment). **Depends on:** #0 (per-user DB isolation — intact) and the single-token Akahu integration shipped earlier.
**Goal:** Stop the live cross-user disclosure on the Bank Feeds page. The Akahu integration is **single-tenant** — one global app/user token pair in `.env` connecting one real person's bank accounts — but the page renders those external accounts **and their bank-reported balances to every logged-in user**, and lets any user link/sync them. Gate the entire external-feed surface behind a single configured **connection owner**, and **fail closed** when no owner is configured.

**Definition of "correct" (the invariant this compartment guarantees):**
> The Akahu connection's external accounts, balances, and sync/link actions are visible and reachable **only** to the configured connection owner. Every other user sees no external bank data and cannot reach any feed-mutating route. If no owner is configured, the external surface is exposed to **no one**.

This is enforced at the router boundary (the page short-circuits before any Akahu fetch; mutating routes 404 for non-owners) — not merely hidden in the template.

---

## 0. Scope summary — the confirmed leak

Verified in code and reproduced by the user (three distinct logged-in users — R&M2, R&MCo, testnewuser — all seeing the same ANZ accounts and balances $6,431.31 / $54,125.89 / $922.18 / $704,874.79):

- **`GET /bank-feeds` (`bank_feeds_page`, bank_feeds.py:57)** calls `akahu_fetch_accounts()` with **no user parameter** and renders the raw external accounts + bank-reported balances (`balance.current` / `available`) to **every** user via `bank_feeds/index.html`.
- **`POST /bank-feeds/link` (:117)** lets **any** user attach **any** external Akahu account to one of their local accounts (the uniqueness check at :135 is global — first-come claims it — but nothing restricts *who* may claim).
- **`POST /bank-feeds/sync` (:173)** and **`POST /bank-feeds/sync-transactions/{id}` (:214)** pull external data using the global token; reachable by any user (sync is scoped to the caller's own local accounts, so the gate that matters is *who may link/sync at all*).
- **Root cause:** the credential is global (`config.py`: `AKAHU_APP_TOKEN` / `AKAHU_USER_TOKEN`), not per-user. Compartments #0/#1 isolated per-user **local DB** data (verified intact: `get_accounts`, `balances_for`, auth all `user_id`-scoped); they never addressed the **shared external credential**. Different class of problem.

Out of band (done): the live `AKAHU_USER_TOKEN` was treated as compromised and **rotated** by the user.

---

## 1. The ownership model

One global token ⇒ one logical owner of the connection. Make that explicit and config-driven (consistent with the token already living in `.env`).

- **New setting** `AKAHU_OWNER_EMAIL: str = ""` (`app/config.py`). The email of the single user who owns the Akahu connection.
- **New helpers** in `app/services/akahu.py` (next to `is_configured`):
  - `owner_email() -> str | None` — normalised (`strip().lower()`) owner email, or `None` if blank.
  - `is_owner(user) -> bool` — `True` iff an owner is configured **and** `user.email` (normalised) matches it. Empty/unset owner ⇒ **always `False`** (fail closed).
- **Fail-closed default:** with Akahu configured but `AKAHU_OWNER_EMAIL` unset, **no** user is the owner, so the external surface is exposed to no one. The owner restores access by setting one env var. (Documented in the page's restricted card.)

`reference` to the existing `User` model: **none** — no schema change, no migration. Ownership is config, matching the single global credential.

---

## 2. Router changes (`app/routers/bank_feeds.py`)

- **`require_owner` dependency** wrapping `require_user`:
  ```python
  async def require_owner(user: User = Depends(require_user)) -> User:
      if not akahu_is_owner(user):
          raise HTTPException(status_code=404)   # foreign resource -> 404, per #0 convention
      return user
  ```
- **`GET /bank-feeds` (`bank_feeds_page`)** keeps `require_user` (every user still gets a page) but:
  - computes `feed_owner = akahu_is_owner(user)`;
  - **only** calls `akahu_fetch_accounts()` when `configured and feed_owner` — non-owners never trigger an external fetch;
  - passes `feed_owner` to the template; non-owners (and the unset-owner case) get a **restricted card**, never external accounts/balances/link forms.
- **`POST /bank-feeds/link`, `/unlink/{id}`, `/sync`, `/sync-transactions/{id}`** switch their `Depends(require_user)` to `Depends(require_owner)` ⇒ **404 for non-owners** (no enumeration, consistent with compartment #0). Their internal logic is otherwise unchanged.

## 3. Template change (`app/templates/bank_feeds/index.html`)

- New branch: `{% elif not feed_owner %}` → a card explaining bank feeds are managed by the household's connection owner (and, when configured-but-no-owner, that `AKAHU_OWNER_EMAIL` must be set). This branch renders **no** external account data.
- Header "Sync All Balances" button guard becomes `{% if configured and feed_owner and not akahu_error %}`.

---

## 4. Risks & mitigations

- **Owner locks themselves out** by not setting `AKAHU_OWNER_EMAIL`. Mitigation: the restricted card names the exact env var; fail-closed is the deliberate, safe default.
- **Stale links on non-owner accounts** (a non-owner who linked before this fix). Harmless: `link_map` is built from the *current* user's own local accounts, and the owner only ever sees their own; mutating routes are now owner-only. Left in place (non-destructive); optional cleanup later.
- **Still single-tenant.** This is **containment**, not multi-tenancy. A genuinely shared household connection where several users each need *their own* bank's accounts requires per-user Akahu credentials — explicitly **out of scope** (§6), tracked as the structural follow-up.

---

## 5. Test plan (ships with the PR) — `tests/test_bank_feed_isolation.py`

- **Non-owner page hides external data:** owner configured, `fetch_accounts` mocked; `GET /bank-feeds` as a non-owner → 200, external account name/balance **absent**, restricted card present, and `fetch_accounts` **never called**.
- **Owner page shows external data:** same setup, `GET` as the owner → external account rendered, `fetch_accounts` called.
- **Mutating routes 404 for non-owner:** `POST /link`, `/unlink/{id}`, `/sync`, `/sync-transactions/{id}` as a non-owner → **404**.
- **Fail closed when owner unset:** tokens set, `AKAHU_OWNER_EMAIL=""`; `GET` as any user → restricted card, no external fetch; `POST /link` → 404.
- **Owner match is normalised:** `AKAHU_OWNER_EMAIL` with mixed case / surrounding space still matches the user's email (`is_owner` unit + page render).
- Full suite stays green.

---

## 6. Acceptance criteria (done = all true)

1. The Bank Feeds page renders external Akahu accounts/balances **only** to the configured owner; every other user sees a restricted card and **no** external data.
2. No external Akahu fetch occurs for a non-owner request (verified by mock-not-called).
3. `link` / `unlink` / `sync` / `sync-transactions` return **404** for non-owners.
4. With `AKAHU_OWNER_EMAIL` unset, the external surface is exposed to **no one** (fail closed) and mutating routes 404 for everyone.
5. Owner matching is case-/whitespace-insensitive on email.
6. New tests per §5 pass; the full suite passes.

---

## 7. Out of scope (follow-up)

- **Per-user Akahu credentials / true multi-tenancy** — each user connects and sees only their own bank's accounts. The structural fix; a later compartment.
- Reconciliation semantics (#3), reporting (#4).
- A UI to set/transfer the owner (config-only for now).

---

## 8. Decisions — LOCKED 2026-06-13

1. **Containment via a single config-defined owner** (`AKAHU_OWNER_EMAIL`), matching the single global credential. No `User` schema change.
2. **Fail closed:** unset owner ⇒ external surface exposed to no one.
3. **Non-owner mutating routes → 404** (not 403), consistent with compartment #0's no-enumeration convention.
4. **Ships as one PR** on `spec-02b-bank-feed-isolation`: config + helpers + router gate + template + tests.
5. **True per-user multi-tenancy is deferred** to a later compartment; this PR only stops the live disclosure.
