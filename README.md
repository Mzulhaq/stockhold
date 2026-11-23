# StockHold

**In progress — milestone 1: runnable inventory foundation.** The current API creates and reads fixed inventory counters. Reservation lifecycles, expiry recovery, concurrency demonstrations and the final verification report are planned in subsequent increments; they are not implemented yet.

StockHold will demonstrate how SQL transactions prevent competing requests from allocating the same inventory. See the [design and milestone plan](docs/design.md). It is a local educational service using synthetic products, with no authentication, payment processing or production deployment.

## Run the foundation

Requires Python 3.11 or 3.13. From the repository root:

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
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
