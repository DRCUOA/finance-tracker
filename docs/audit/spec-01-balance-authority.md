# Spec — Compartment #1: Authoritative Model + Single Balance Authority

**Status:** Ready to build. **Depends on:** nothing (can run in parallel with #0 security).
**Goal:** Make the transaction ledger the single source of truth for all balances. Balances are **derived on read** via one function. Remove the `current_balance` cache and the 6 divergent re-implementations.

**Definition of "correct" (the invariant this compartment guarantees):**
`balance(account, basis=ALL) == initial_balance + Σ(amount for that account)` — true by construction, because there is no cache to drift.

---

## 1. The single API

New module `app/services/balances.py`:

```python
class BalanceBasis(str, enum.Enum):
    ALL = "all"          # every transaction (pending + posted)
    POSTED = "posted"    # is_pending = False  -- DEFAULT for headline balance & net worth
    CLEARED = "cleared"  # is_cleared = True  -- strict: confirmed on a finalised
                         #   statement reconciliation; cleared ⊆ posted (see audit §4)

async def balance(
    session, account_id: uuid.UUID, *,
    basis: BalanceBasis = BalanceBasis.POSTED,
    as_of: date | None = None,   # inclusive upper bound on transaction date
) -> Decimal:
    """initial_balance + Σ(amount) for the account, filtered by basis (and as_of)."""

async def balances_for(
    session, user_id: uuid.UUID, *,
    basis: BalanceBasis = BalanceBasis.POSTED,
    as_of: date | None = None,
) -> dict[uuid.UUID, Decimal]:
    """Bulk variant — one grouped query for all of a user's accounts. Avoids N+1."""

async def net_worth(
    session, user_id: uuid.UUID, *,
    as_of: date | None = None,
) -> NetWorth:   # assets, liabilities, net (uses Account.group for asset/liability split)
```

Implementation: a single `select(func.sum(Transaction.amount))` grouped by `account_id`, with the `basis` translated to the WHERE clause **in exactly one place**. `initial_balance` added from the account row. `as_of` adds `Transaction.date <= as_of`.

**Decision (LOCKED 2026-06-11):** default `basis = POSTED` for the headline account balance **and** net worth. This preserves today's headline behaviour (the old cache excluded pending). The reports net-worth chart currently includes pending (`ALL`) and **will be switched to `POSTED`** for consistency (see §3) — a deliberate, tested behaviour change.

---

## 2. Data-model change

Migration (new alembic revision):
- **Drop** `accounts.current_balance`.
- No other column changes in this compartment.

Reversible: the down-migration re-adds the column and backfills via `initial_balance + Σ(posted)`.

---

## 3. Call sites to migrate

**Delete these re-implementations, replace with `balance(...)`:**

| Site | Today | New basis |
|---|---|---|
| `services/accounts.py:100-122` `recalculate_balance` | writes cache | **delete entirely** (no cache) |
| `services/accounts.py:64,120` (create) | seeds cache | delete cache writes |
| `services/reconciliation.py:14-22` `get_cleared_balance` | `is_cleared` sum | `balance(basis=CLEARED)` |
| `services/feed_reconciliation.py:148-163` `posted_balance` | posted sum | `balance(basis=POSTED)` |
| `services/printable_statement.py:285-306` `_opening_balance` | posted, `date<` | `balance(basis=POSTED, as_of=before-1)` |
| `services/reports.py:326-330` `net_balance_history` | **all rows** sum | `balance(basis=POSTED)` — **switch off "include pending"** to match the headline (decision locked) |
| `services/migration.py:429` | all-rows cache seed | delete cache write |
| `services/backup.py:128,328,371` | cache seed/recompute | delete cache writes |

**Switch these reads from cache → function:**

| Site | Today | New |
|---|---|---|
| `routers/dashboard.py:63-65` net worth tile | `sum(current_balance)` | `net_worth(user_id)` |
| `routers/accounts.py:37-38` net worth | `sum(current_balance)` | `net_worth(user_id)` |
| account list / detail display of per-account balance | `current_balance` | `balances_for(user_id)` (bulk) |
| templates: `dashboard/index.html:214`, `accounts/list.html:117,178`, `reconciliation/index.html:25`, `bank_feeds/index.html:174,228` | render `current_balance` | render value passed from router via the function |

**Interest (special handling):**
- `services/interest.py:244,423` currently do `current_balance += amount`. With no cache, the accrual must take effect **only** by inserting an interest transaction into the ledger (verify the accrual already creates a transaction; if so, the `+=` is pure redundancy and is deleted).
- `interest.py:164,213` read `current_balance` for skip-check / principal sizing → replace with `balance(account_id, basis=POSTED)`.

---

## 4. Risks & mitigations

- **Performance:** per-request balance sums instead of a column read. At "small but real" scale this is trivial; mitigate N+1 with the bulk `balances_for` / `net_worth` grouped queries. Ensure an index on `transactions(account_id)` exists (it does via FK; confirm a covering index on `(account_id, date)` for `as_of` queries).
- **Pending-basis behaviour change (decided):** the reports net-worth chart switches from "include pending" to `POSTED`. This is a visible number change for accounts with pending rows; ship a test pinning the new `POSTED` behaviour and note it in the changelog.
- **Concurrency:** removing the cache *eliminates* the read-modify-write races; net positive.

---

## 5. Test plan (ships with the PR)

- **Property test:** random ledger → `balance(ALL) == initial_balance + Σ(amount)`; `POSTED`/`CLEARED` honour filters; `as_of` boundary correct (inclusive).
- **Cross-screen agreement:** dashboard, accounts, reports net worth all equal `net_worth()` for the same fixture.
- **Interest:** accrual changes the derived balance by exactly the inserted transaction amount and nothing else.
- **Regression:** existing balance-related tests pass after cache removal.
- **No-cache guard:** `grep -r current_balance app/` returns nothing.

---

## 6. Acceptance criteria (done = all true)

1. `app/services/balances.py` is the **only** implementation of `initial_balance + Σ(transactions)`.
2. `accounts.current_balance` column removed; no code/template references it.
3. Property test green; the §-invariant holds for random ledgers.
4. Dashboard, accounts, and reports net-worth values are identical (same function).
5. Interest accrual has a single effect: the ledger transaction.
6. All existing tests pass; new tests added per §5.

---

## 7. Out of scope (later compartments)

- Dedup / ingestion integrity → compartment #2.
- `reported_balance` reconciliation semantics → compartment #3.
- Reporting consolidation beyond net worth → compartment #4.
- ~~The chosen `basis` default and the reports-pending behaviour are decisions to confirm at review~~ — **decided 2026-06-11: default `POSTED` for headline & net worth; reports chart switches to `POSTED`.**
- The dedup-override path for legitimate repeats → compartment #2 (see audit §4 & §6).
