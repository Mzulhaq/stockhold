# StockHold

**In progress — milestone 2: reservation lifecycle.** The API supports inventory, holds, idempotent request replay, confirmation, cancellation and expiry checks on actions. A bounded expiry sweeper, invariant audit command, concurrency demonstrations and the final verification report are planned for subsequent increments.

StockHold uses SQL transactions to preserve inventory while reservations change state. See the [design and milestone plan](docs/design.md). It is a local educational service using synthetic products, with no authentication, payment processing or production deployment.

## Run the foundation

Requires Python 3.11 or 3.13. From the repository root:

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
stockhold serve
```

Open http://127.0.0.1:8000/docs. `POST /v1/products` takes `{"sku":"DEMO-ITEM","quantity":10}`; `GET /v1/products/DEMO-ITEM` returns total, available, reserved and committed counters. Valid SKU characters are uppercase letters, digits, `_` and `-` (1–48 characters). Quantities must be integer 1–1,000,000. Duplicate SKUs conflict; inputs are validated without coercing booleans or strings into quantities. POST requires JSON and accepts at most 4 KiB.

Default database: `sqlite:///stockhold.db`. Override with `STOCKHOLD_DATABASE_URL` or `stockhold serve --database-url ...`; `.env.example` documents placeholders and is not loaded automatically. The process binds loopback by default. Do not expose this unauthenticated teaching service publicly.

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Tests use temporary SQLite databases by default. `TEST_DATABASE_URL` optionally selects a disposable PostgreSQL database; tests delete their own test tables during setup/teardown, so use a dedicated test database. CI exercises both engines. Never point tests at data you need to keep.

The project is developed with substantial AI assistance. Commits are actual tested milestones with real timestamps. Features and measurements will be documented as they are implemented and verified.

## Reserve, confirm and cancel

`POST /v1/reservations` accepts `{"sku":"DEMO-ITEM","quantity":2,"ttl_seconds":300}` with an `Idempotency-Key: order-001` header. TTL defaults to 300 seconds and must be an integer 1–3,600. Keys are 1–64 ASCII letters, digits, hyphens or underscores, scoped to the SKU.

The response is `{"reservation":{...},"replayed":false}` with HTTP 201 for a new hold. Repeat the same request and key to get the existing reservation with HTTP 200 and `replayed:true`. Reusing the key with a different quantity or TTL returns 409. Keys persist after terminal states; a replay never creates a fresh allocation.

`GET /v1/reservations/{id}` reads the persisted state. `POST /v1/reservations/{id}/confirm` or `/cancel` accepts an empty body or `{}`. Confirming transfers reserved units to committed; cancelling returns them to available. Repeating the same completed action is idempotent. A contradictory terminal action returns 409. The invariant is `available + reserved + committed = total`, with all counters nonnegative.

Database time is sampled after acquiring the product lock. At `expires_at <= now`, a held reservation cannot be confirmed: the operation commits its expiry and stock release, then returns an `expired` conflict. Replaying that hold returns its expired state. Reads do not expire rows, so an abandoned hold can remain persisted as `held` until an action or future sweep processes it. This milestone does not run a background sweeper.

Each mutation locks the product before changing its reservations, counters and append-only audit rows in one transaction. PostgreSQL serializes mutations on that SKU; SQLite serializes writes across the database. A lock timeout or database failure returns 503 without SQL or credentials. This is a correctness-oriented design with no hot-SKU throughput claim. No client-supplied clock or process-local mutex controls correctness.
