# Validation evidence

## Local verification — November 23, 2026

Executed on Windows with Python 3.11.9. Commands below were run from the repository root, using its virtual environment unless noted.

| Command or check | Actual result |
| --- | --- |
| `python -m pytest --cov=stockhold --cov-report=term-missing` | 79 passed; 94% parent-process package statement coverage (340 statements, 21 missed). Spawned child-process execution is not included in that coverage figure. |
| `python -m ruff check .` and `python -m ruff format --check .` | Passed |
| `python scripts/demo.py` | Real loopback HTTP contention, replay, transitions and expiry checks passed; final audit had no violations |
| `python -m build` | Built source archive and platform-independent wheel |
| New environment: `python -m pip install -r requirements-runtime.lock`, then `python -m pip install --no-deps dist/stockhold-0.1.0-py3-none-any.whl` | Installed successfully; imported package from the clean environment, not editable source |
| `python -m pip check` in clean environment | No broken requirements |
| `python scripts/demo.py` using clean wheel environment | Passed with the same scenario counts and audit results |
| Clean-wheel CLI server and HTTP requests using checked-in example JSON | Server started; product and reservation creation succeeded; process stopped |
| Publication scan | 30 tracked/staged files scanned; no credential or private-path patterns found. Request examples are synthetic. |

The HTTP demo submitted 16 one-unit requests against five units: five holds succeeded and eleven returned stock conflicts. Twelve requests with the same key produced one hold and eleven replays. Expiry recovery released two units once. The final audit checked three products, seven reservations and twelve transition records. Final totals across the products were four available units, two reserved and four committed, conserving all ten units. These are reproducible correctness scenarios, not latency/throughput or production-impact measurements.

Concurrency tests additionally use independent engines and four spawned processes. Fault-injection checks verify that an audit-write failure rolls back reservation and stock updates together. The snapshot audit is tested against deliberate counter/history drift, and timestamps beyond the 2038 signed-32-bit boundary are exercised. Independent code/documentation review found no unresolved material issues.

## Remote verification

The complete workflow now includes SQLite on Python 3.11/3.13, PostgreSQL 17 on Python 3.11, Windows on Python 3.11, and the nonroot Docker demo with external networking disabled. Docker is not installed locally; the container run must pass remotely before completion.

The reservation and recovery implementation passed 78 local tests. PostgreSQL CI initially exposed a multiprocessing test-harness bug: converting a SQLAlchemy URL to text masked its password. The fix passes the picklable URL object in memory without logging or saving credentials. [Run 45282568554](https://github.com/Mzulhaq/stockhold/actions/runs/45282568554) then passed all three SQLite/PostgreSQL jobs at commit `16f8d046175f440468bb565e5384b1fd8a8ec130`. The earlier failed run remains in Git history; it was not hidden or rewritten.
