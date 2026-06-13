# Spec — Compartment #2: Transaction Ingestion Integrity

**Status:** Ready to build. **Depends on:** #1 (merged — the ledger is now the single balance authority, so dedup correctness directly determines balance correctness).
**Goal:** One canonical transaction identity and **one** dedup routine, backed by **DB-level uniqueness that holds for every source** (manual, CSV, OFX, Akahu). Kill the 3 copy-pasted dedup rules, fix the CSV reused-reference silent-drop, and add an explicit, audited override path for genuine repeats.

**Definition of "correct" (the invariant this compartment guarantees):**
No accidental duplicate and no silently-dropped row can enter the ledger. The *only* way a second row with identical content enters is via an explicit, recorded override. Formally, for a given `account_id`:

> at most one transaction exists per `(content_hash, occurrence)`, where `occurrence > 0` exists **iff** a user deliberately admitted a repeat (`dedup_override = true`).

This is DB-enforced (a unique constraint), not merely checked in Python — so it survives races, concurrent imports, and any future call site.

---

## 0. Scope summary (audit §1.4, §5, §6, §8)

Today (verified in code):

- **Dedup logic copy-pasted 3×**, all with the same two-tier `reference`-then-`date+amount+lower(trim(desc))` rule: `import_service._is_duplicate` (304), `import_service.find_duplicates` (335), `transactions.check_duplicate` (111).
- **DB uniqueness is inconsistent by source:**
  - Akahu → `uq_transactions_source_akahu_tx_id` partial unique on `(source, akahu_transaction_id)` (migration 010). **Keep.**
  - CSV / null-source → `ix_transactions_account_reference_dedupe` partial unique on `(account_id, reference)` (migrations 003→015→019), excluding `manual` and `akahu`.
  - **Manual → no constraint at all.**
  - The content tier (`date+amount+desc`) is **never** DB-enforced for any path.
- **CSV reused-reference silently drops genuine transactions** — the reference-first rule treats two distinct payments that share a bank-populated reference as duplicates. (This is the OFX/CSV analogue of the bug fixed for Akahu in migration 019.)
- **Re-importing a CSV with a blank reference races** between the `SELECT` dup-check and the `INSERT` → double count (no DB backstop).
- **No cross-source dedup** — the same real payment from Akahu *and* a CSV both persist → double count.

---

## 1. The single module

New module `app/services/dedup.py` — the **only** place that decides transaction identity or duplicate status.

```python
# app/services/dedup.py
import hashlib
import re
from datetime import date
from decimal import Decimal

_WS = re.compile(r"\s+")

def normalise_description(s: str) -> str:
    """Lower-case, strip, collapse internal whitespace. Deterministic and
    source-agnostic so a CSV ' | '-joined description and the same text typed
    by hand hash identically."""
    return _WS.sub(" ", (s or "").strip().lower())

def content_hash(d: date, amount: Decimal, description: str) -> str:
    """Account-independent digest of the *content* of a transaction.
    account_id is the leading column of the unique constraint, not part of the
    hash. Amount keeps its sign and is quantised to 2dp so 1.5 == 1.50."""
    amt = f"{amount.quantize(Decimal('0.01')):f}"
    payload = f"{d.isoformat()}|{amt}|{normalise_description(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

async def find_existing(session, account_id, ch: str) -> list[Transaction]:
    """All rows for this account sharing the content hash, ordered by occurrence."""

async def is_duplicate(session, *, account_id, d, amount, description) -> bool:
    """The ONE dedup check: True iff an occurrence-0 row with this content_hash
    already exists for the account. `reference` is deliberately NOT a signal
    (that is the CSV reused-reference bug). Akahu identity is handled separately
    by (source, akahu_transaction_id), not here."""

async def next_occurrence(session, account_id, ch: str) -> int:
    """max(occurrence)+1 for this (account, content_hash) — the discriminator
    used when an override admits a genuine repeat."""

class Admission:
    insert: bool          # False => caller skips (accidental duplicate)
    content_hash: str
    occurrence: int       # 0 normally; >0 only via override
    dedup_override: bool

async def admit(session, *, account_id, d, amount, description,
                override: bool = False) -> Admission:
    """The single entry point every insert path calls. Computes content_hash;
    if no occurrence-0 row exists -> admit at occurrence 0. If one exists:
      - override=False -> reject (insert=False), the accidental re-import case;
      - override=True  -> admit at next_occurrence, dedup_override=True (audited)."""
```

`reference` remains a stored data column (useful for display and for the user) but is **no longer** part of identity or the dedup decision.

---

## 2. Data-model change

New alembic revision `022` (head is `021`). All additive + one index swap; reversible.

**Columns on `transactions`:**
- `content_hash` `String(64)`, nullable→backfilled→`NOT NULL`, indexed `(account_id, content_hash)`.
- `occurrence` `SmallInteger`, `NOT NULL`, server_default `0`.
- `dedup_override` `Boolean`, `NOT NULL`, server_default `false` — the **audit marker** recording that a row was admitted as a deliberate repeat.

**Constraints:**
- **Add** `uq_transactions_account_content_occurrence` — `UNIQUE (account_id, content_hash, occurrence)`. This is the new cross-source backstop covering **all** sources incl. manual.
- **Keep** `uq_transactions_source_akahu_tx_id` (Akahu's stable-id identity).
- **Drop** `ix_transactions_account_reference_dedupe` — reference is no longer the dedup signal, so its uniqueness is superseded (and was the source of the reused-reference drop). Reversible: the down-migration re-creates it as in migration 019.

**Backfill (LOCKED decision §8.2 — auto-discriminate, keep all):**
Run in **Python inside the migration** (via `op.get_bind()`), importing `app.services.dedup` so the backfill hashes **byte-identically** to runtime (a raw-SQL reimplementation would risk normalisation drift). For each `account_id`:
1. Compute `content_hash` for every row.
2. Group by `content_hash`; order each group by `created_at, id`.
3. Assign `occurrence = 0, 1, 2, …` within the group. Any row with `occurrence > 0` (a pre-existing duplicate) is also set `dedup_override = true` — we treat existing dupes as intentional and **keep every row** (nothing deleted).
4. Set `content_hash NOT NULL`, then create the unique constraint (now guaranteed to hold).

---

## 3. Call sites to migrate

**Delete these re-implementations, replace with the `dedup` module:**

| Site | Today | New |
|---|---|---|
| `import_service._is_duplicate` (304) | ref-then-content, two-tier | **delete** → `dedup.is_duplicate(...)` |
| `import_service.find_duplicates` (335) | ref set + content loop, sets `is_duplicate` flag for the preview | **rewrite** on `dedup.is_duplicate` (preview marker only) |
| `transactions.check_duplicate` (111) | ref-then-content, `exclude_id` support | **delete** → `dedup.is_duplicate(...)` (+ `exclude_id` for the edit path) |

**Route every insert through `dedup.admit(...)`:**

| Site | Change |
|---|---|
| `transactions.create_transaction` (149, `force=`) | **Generalise `force` into the override path.** `force=True` → `admit(..., override=True)` → sets `occurrence`/`dedup_override`. `force=False` → `admit(...)`; on `insert=False` raise `DuplicateTransactionError` as today. Always write `content_hash`/`occurrence`/`dedup_override`. |
| `import_service.import_statement_lines` (420) | Replace the `_is_duplicate` skip with `admit(...)`. The confirm UI may pass a set of line ids the user chose to **keep as repeats** → those call `admit(override=True)`; the rest skip on collision. (No new screens required for the §8 acceptance — distinct-content rows already both insert because reference isn't identity.) |
| `transactions.update_transaction` (182) | If `date`/`amount`/`description` change, recompute `content_hash` and re-check via `dedup.is_duplicate(exclude_id=self)`. |
| `akahu.sync_account_transactions` (insert branch, ~446) | New Akahu rows also compute & store `content_hash`/`occurrence=0`. **Feed never overrides** (§8.3). **Cross-source adoption rule:** if a new Akahu row content-collides with an existing **non-feed** row, the sync **adopts** that row (writes `source='akahu'` + `akahu_transaction_id`/`akahu_account_id`/`akahu_updated_at` onto it) instead of inserting a second row — so the same real payment isn't double-counted and no Akahu metadata is lost. The existing `(source, akahu_transaction_id)` upsert path is unchanged for rows already carrying an Akahu id. |

**Concurrency backstop:** every insert path wraps the flush so a `UniqueViolation` on `uq_transactions_account_content_occurrence` is caught and treated as a duplicate (skip / `DuplicateTransactionError`). This is what finally closes the "race between SELECT and INSERT" double-count — the DB, not the Python check, is the guarantee.

---

## 4. Risks & mitigations

- **Backfill ↔ runtime hash drift.** Mitigated by doing the backfill in Python through the *same* `dedup` module (§2), and a test that re-hashes a fixture both ways.
- **Pre-existing duplicates.** Locked decision: auto-discriminate and keep all — non-destructive, every current row survives with an `occurrence`/`dedup_override` (§2/§8.2).
- **Cross-source enriched-description misses.** Akahu often rewrites descriptions, so a true Akahu-vs-CSV match can still slip the content hash. **Accepted** (locked §8.1): this is a visible, correctable double-count, never a silent merge. The adoption rule (§3) catches the exact-match case.
- **Reference-bearing imports that genuinely relied on ref-uniqueness.** Distinct-content rows now both insert (correct — fixes the drop); identical-content accidental re-imports are still blocked by content. Net behaviour is strictly safer.
- **Concurrency.** The unique constraint is the backstop; `IntegrityError` is caught and mapped to "duplicate". Removing the reference-uniqueness index removes a class of whole-batch rollbacks (cf. migration 019 rationale).

---

## 5. Test plan (ships with the PR)

- **Idempotent re-import:** importing the same CSV/OFX batch twice adds rows the first time, **zero** the second.
- **Reused reference, distinct payment:** two rows sharing a `reference` but differing in date/amount/description → **both** inserted (the bug fix).
- **Override path:** two identical-content rows same day — without override the second is rejected; with override it is admitted at `occurrence=1`, `dedup_override=true`, both present and the override is auditable.
- **Feed never overrides:** `sync_account_transactions` offers no override; an Akahu row content-matching an existing manual row **adopts** it (no second row; Akahu metadata written onto the existing row).
- **Concurrency backstop:** two concurrent inserts of identical content → exactly one survives; the loser is handled as a duplicate, not a 500.
- **Cross-source:** a manual row and an Akahu row of identical content do not double (adoption); enriched-description case documented as accepted double (pinning the known limitation).
- **Migration backfill:** a fixture DB containing pre-existing duplicates → after upgrade every row survives with ascending `occurrence` and `dedup_override` on the repeats; the unique constraint exists and holds.
- **Single-rule guard:** `grep -rn "_is_duplicate\|check_duplicate" app/` returns only the `dedup` module (the 3 copies are gone).

---

## 6. Acceptance criteria (done = all true) — from audit §8

1. **Exactly one** dedup routine; the 3 copies are deleted.
2. DB-level uniqueness holds across **all** sources **including manual** (`uq_transactions_account_content_occurrence`).
3. Re-importing the same CSV twice adds **zero** rows; a CSV with a reused reference but a distinct payment adds **both**.
4. **Dedup-override path:** a user can intentionally admit a genuine repeat; the row is stored distinct, marked `dedup_override`, and remains auditable. Accidental re-import (no override) is rejected. Override is **unavailable on automated feed sync**.
5. The CSV/OFX reused-reference silent-drop is fixed.
6. Migration backfills `content_hash`/`occurrence`/`dedup_override` for all existing rows, keeps every row, and the constraint holds. All prior tests pass; new tests per §5 added.

---

## 7. Out of scope (later compartments)

- `reported`/`posted`/`cleared` reconciliation semantics and the external-correction path → compartment #3.
- Reporting/output consolidation beyond what #1 unified → compartment #4.
- A richer CSV confirm-UI for per-row "keep as repeat" beyond the minimal override wiring → follow-up (the §8 acceptance does not require new screens).

---

## 8. Decisions — LOCKED 2026-06-13

1. **Dedup identity → content key, all sources.** Identity = `account_id` + `date` + `amount` (signed, 2dp) + normalised description, DB-enforced for **every** source incl. Akahu; Akahu additionally keeps its `(source, akahu_transaction_id)` constraint. `reference` is **not** identity. Cross-source matches missed because Akahu enriches descriptions are accepted as visible/correctable, never a silent merge.
2. **Backfill → auto-discriminate, keep all.** Pre-existing collisions get ascending `occurrence` numbers (repeats flagged `dedup_override`); nothing is deleted; the constraint then applies cleanly.
3. **Override → occurrence discriminator + marker.** A deliberate repeat is admitted at `next_occurrence` with `dedup_override = true`; the existing `force=True` on manual create generalises into this. The unique constraint stays total (no partial-index carve-out).
4. **Ships as one PR** — `dedup` module + call-site unification + migration/backfill/constraints together.
