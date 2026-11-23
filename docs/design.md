# StockHold design and delivery plan

StockHold demonstrates a business invariant under concurrent requests: a finite inventory unit can be available, held, or committed, but never allocated twice. The project adds transactional business logic to a portfolio already covering webhooks, ingestion, retrieval and model serving. No real orders, payments, private data or external services are used.

## Scope and invariant

Python 3.11+, FastAPI, SQLAlchemy Core, SQLite for a zero-service local demo, PostgreSQL for concurrent integration tests. A single-SKU reservation moves through `held -> confirmed | cancelled | expired`. A product has fixed `total`, `available`, `reserved`, `committed` integer counters. All must be nonnegative and `available + reserved + committed = total`. A confirmation consumes a held unit permanently; cancellation/expiry return it to availability. No restocking, returns, multi-SKU basket, payments, users, distributed cache or arbitrary job execution.

SKU is uppercase ASCII `[A-Z0-9][A-Z0-9_-]{0,47}`. Initial quantity and each hold quantity are strict integers 1..1,000,000. TTL is a strict integer 1..3,600 seconds, default 300. Idempotency keys are 1..64 ASCII letters/digits/underscore/hyphen, scoped per SKU; reuse with different quantity or TTL conflicts. A replay returns the same reservation, including its current terminal state; it never creates fresh stock allocations. Idempotency records are retained indefinitely in this bounded demo.

All mutation paths lock the product first, then inspect/modify reservations in one transaction. A no-op `UPDATE products SET available=available WHERE sku=:sku` acquires a write lock on SQLite and a row lock on PostgreSQL. After lock acquisition, read current state and database time. Serialization is per SKU on PostgreSQL and database-wide for SQLite writes. This favors a simple correctness argument over hot-SKU throughput. Never use an in-process mutex as the correctness mechanism. Transaction rollback must undo counters, reservation and audit changes together.

Database time is an integer epoch second sampled after acquiring the product lock: SQLite `strftime('%s','now')`; PostgreSQL `floor(extract(epoch from clock_timestamp()))`. A test-only injected callable may substitute a deterministic clock; no HTTP clock override. A hold is expired when `expires_at <= now`; confirmation must check this even if no sweeper has run. Persist the expiry and stock release before returning an expired conflict, rather than throwing inside the transaction and undoing the expiry. Reads report persisted status and expiry time; a scheduled, bounded sweep releases abandoned holds. Each transition writes an append-only audit row in the same transaction, without request bodies or personal information.

## API and storage contract

- `GET /health` checks the database; `POST /v1/products` creates `{sku, quantity}`; `GET /v1/products/{sku}` reports all counters.
- `POST /v1/reservations` accepts `{sku, quantity, ttl_seconds}` plus `Idempotency-Key`; returns `{reservation, replayed}` (201 new / 200 replay).
- `GET /v1/reservations/{id}` reports the persisted reservation; `POST .../{id}/confirm` and `POST .../{id}/cancel` return its state. Repeating the same successful action is idempotent; a contradictory terminal action conflicts.
- Domain errors carry stable codes: not_found (404), sku_exists, insufficient_stock, idempotency_conflict, invalid_transition, expired (409). Validation errors are 422, malformed JSON 400, unsupported media 415, request body >4 KiB 413. Database contention/unavailability returns 503 without SQL or connection credentials.
- CLI `stockhold serve --database-url ... --host 127.0.0.1 --port 8000`; `stockhold expire --database-url ... --limit 100`; `stockhold audit --database-url ...`. Environment default `STOCKHOLD_DATABASE_URL=sqlite:///stockhold.db`.
- Foundation `Database(url)` creates an engine and v1 schema; `close()` disposes it; `ping()`, `create_product(sku,quantity)`, `get_product(sku)` return JSON-compatible mappings. `DomainError(code,message,status=409)` is safe for HTTP clients. Store uses an explicit schema-version table and fails closed on unknown versions; no silent migration of existing data. Future methods `reserve(sku,quantity,ttl_seconds,key)`, `get_reservation(id)`, `transition(id,action)`, `expire(limit)`, `audit()`.

Local use only: loopback default, no authentication, rate limits or internet deployment. No TLS/tenant authorization claims. Request body bounds apply even without Content-Length. Database URLs belong in local environment variables; committed examples use placeholders or an intentionally disposable CI password.

## Actual milestone workflow

1. Foundation: design, runnable product API, SQL schema, CLI, input validation, tests and minimal SQLite/PostgreSQL CI. Publish only after relevant checks pass. Mark in progress.
2. Reservation lifecycle: implement holds, per-SKU idempotency, confirmation/cancellation, expiry checks on actions and audit transitions; add tests and push the working increment.
3. Recovery and concurrency: implement bounded expiry sweep and invariant audit; exercise competing holds, duplicate keys, terminal-action races and rollback with independent connections on SQLite/PostgreSQL. Push after checks pass.
4. Demo and review: real HTTP synthetic inventory scenario, Docker, complete README, architecture/interview notes/exercise, independent review and fixes. Push and verify remote CI.
5. Evidence: record actual demo counts, checks and CI results; verify final pushed commit and complete the local ledger. No backdating, fabricated bugs or artificial delays.

Every increment records scope, actual commit/push time, validation and CI in the local ledger. This plan describes intended behavior; README status identifies what is currently implemented. AI assistance is disclosed. Do not count an in-progress foundation as the completed weekly deliverable.
