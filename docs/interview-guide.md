# StockHold interview guide

StockHold demonstrates inventory reservations through SQL transactions. A product has fixed integer counters; a reservation moves from `held` to `confirmed`, `cancelled`, or `expired`. Confirmation moves units from reserved to committed. Cancellation and expiry return them to available stock. Every successful transition and its audit row commit with the counter changes.

## Architecture walkthrough

The API validates SKU, quantity, TTL, JSON shape, and idempotency key before calling the store. A mutation locks the product row first, reads current state and database time, then updates reservation, product counters, and audit history in one transaction. The database constraint enforces the product equation `available + reserved + committed = total`, while the audit command checks those counters against reservation states and checks transition history. The same lock order serializes conflicting operations on a SKU. PostgreSQL locks the product row; SQLite serializes database writes. The expiry command processes a bounded candidate list and commits each reservation separately.

## Interview questions and answer notes

### 1. What is the difference between the counter equation and the full inventory invariant?

The database constraint enforces nonnegative counters and `available + reserved + committed = total` for each product row. That equation alone cannot prove that `reserved` equals the quantity of all currently held reservations or that `committed` matches confirmed reservations. `audit()` cross-checks reservation totals and transition history. Those relationships span tables and are audited by the application rather than enforced as one database constraint.

### 2. Why lock the product before touching a reservation?

Every mutation for a SKU first updates the product row to itself, acquiring the database lock before reading or changing reservation state. PostgreSQL serializes by product row, so unrelated SKUs can proceed independently. SQLite serializes writes across the database. This order gives competing holds and state transitions one consistent path and avoids relying on an in-process mutex, which would not coordinate separate processes.

### 3. What happens when an idempotency key is replayed?

The key is unique within a SKU. If the key is reused with a different quantity or TTL, the request conflicts. If the input matches, the existing reservation is returned with its current state, including a terminal state. A replay never allocates stock again. Retaining idempotency records indefinitely makes that guarantee straightforward for this bounded demo, but there is no pruning policy yet.

### 4. How does expiry interact with a confirm request?

The store acquires the product lock and samples database time after the lock. A hold expires when `expires_at <= now`. If confirmation discovers an expired hold, it releases the units, updates the reservation, and appends the expiry audit row in the transaction. Only after the transaction commits does it return the `expired` conflict. Raising the error before commit would roll back the expiry and stock release.

### 5. What does the audit command prove?

It checks one consistent read snapshot against retained reservation and event records. It can detect counter drift or a mismatched transition history. Audit rows are append-only through the application, but the database is not a cryptographically tamper-proof ledger: an administrator with direct write access could alter both records and checks. The audit scans retained rows in memory and is intended for this small project.

### 6. Why is expiry bounded and partially committed?

The sweep selects at most the requested limit, ordered by expiry and reservation ID. It then locks each reservation's product, rechecks status and database time, and commits that release separately. This prevents one long batch transaction and means a failure can leave earlier releases committed while later candidates remain for the next run. Retrying is safe because each candidate is rechecked under the lock. A scheduler is not started automatically.

### 7. How do the concurrency and fault-injection tests help?

Concurrency tests use barriers and separate database engines; one test also starts independent processes. They check business outcomes rather than timing or throughput. In the SQLite and PostgreSQL tests, 16 one-unit requests compete for five units: five succeed and eleven receive insufficient-stock conflicts. Fault injection raises while inserting an audit row after business updates have begun, then checks that stock, reservation, and audit changes all roll back together. PostgreSQL verification passed in [CI run 45282568554](https://github.com/Mzulhaq/stockhold/actions/runs/45282568554). These results are not a production load claim.

## 30-minute exercise: cap active holds per SKU

Add an operator-configured limit on the number of currently held reservations for each SKU. Use the existing product lock as the serialization point so two requests cannot both pass a stale count check. Add a focused test where concurrent requests attempt to cross the limit and prove that accepted holds never exceed it; also check that cancelled or expired holds free capacity and that a same-key replay does not consume another slot. Do not implement the exercise as part of this guide.

Hints for discussion: define whether the limit counts reservations or reserved units, and make the rule explicit before coding. Query the current held count only after obtaining the product lock. Keep the check, reservation insert, stock update, and audit insert in the existing transaction. Test the boundary, replay, and rollback behavior.

## Resume drafts

Adapt these drafts to the work you personally completed and can explain. See [validation evidence](validation.md) for actual test counts and verified environments.

- Implemented a SQL-backed inventory reservation lifecycle with per-SKU idempotency, expiry handling, and transactional counter updates that preserve the stock-conservation equation.
- Added local concurrency and rollback checks; in a 16-request SQLite correctness test competing for five units, five holds succeeded and eleven were rejected for insufficient stock. This is a test scenario, not a production benchmark.

AI assistance contributed substantially to this project. Describe your actual role accurately, review the implementation yourself, and be ready to explain the transaction and concurrency choices. No production deployment or business impact is claimed.
