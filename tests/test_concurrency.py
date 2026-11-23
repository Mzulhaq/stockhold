from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, func, select

from stockhold.store import Database, DomainError, audit_events, reservations


def _make_workers(db, count, clock=None):
    """Create independent engines sequentially before any contenders start."""
    return [Database(db.engine.url, clock=clock) for _ in range(count)]


def _reserve_after_barrier(database, barrier, key, quantity=1, ttl=60):
    barrier.wait(timeout=30)
    try:
        result = database.reserve("RACE", quantity, ttl, key)
        return "ok", result
    except DomainError as error:
        return "error", error.code


def _race_reservations(databases, args):
    barrier = Barrier(len(databases))
    with ThreadPoolExecutor(max_workers=len(databases)) as pool:
        futures = [
            pool.submit(_reserve_after_barrier, database, barrier, *argument)
            for database, argument in zip(databases, args, strict=True)
        ]
        return [future.result(timeout=60) for future in futures]


def _audit_count(db, reservation_id, to_state=None):
    statement = (
        select(func.count())
        .select_from(audit_events)
        .where(audit_events.c.reservation_id == reservation_id)
    )
    if to_state is not None:
        statement = statement.where(audit_events.c.to_state == to_state)
    with db.engine.connect() as connection:
        return connection.scalar(statement)


def _process_reserve(database_url, key):
    """Process-pool worker: create and close its own engine inside the child."""
    database = Database(database_url)
    try:
        try:
            result = database.reserve("PROCESS", 1, 60, key)
            return "ok", result["replayed"], result["reservation"]["id"]
        except DomainError as error:
            return "error", error.code
    finally:
        database.close()


def test_competing_unique_holds_never_overallocate_stock(db):
    db.create_product("RACE", 5)
    workers = _make_workers(db, 16)
    try:
        results = _race_reservations(workers, [(f"request-{index}", 1, 60) for index in range(16)])
        successes = [value for kind, value in results if kind == "ok"]
        errors = [value for kind, value in results if kind == "error"]
        assert len(successes) == 5
        assert errors == ["insufficient_stock"] * 11
        assert len({value["reservation"]["id"] for value in successes}) == 5
        assert db.get_product("RACE") == {
            "sku": "RACE",
            "total": 5,
            "available": 0,
            "reserved": 5,
            "committed": 0,
        }
        assert db.audit()["ok"] is True
    finally:
        for worker in workers:
            worker.close()


def test_same_key_requests_from_independent_engines_make_one_reservation(db):
    db.create_product("RACE", 3)
    workers = _make_workers(db, 12)
    try:
        results = _race_reservations(workers, [("same-key", 1, 90)] * 12)
        successes = [value for kind, value in results if kind == "ok"]
        assert len(successes) == 12
        assert sum(not value["replayed"] for value in successes) == 1
        assert sum(value["replayed"] for value in successes) == 11
        ids = {value["reservation"]["id"] for value in successes}
        assert len(ids) == 1
        reservation_id = ids.pop()
        assert db.get_product("RACE")["reserved"] == 1
        assert _audit_count(db, reservation_id) == 1
        assert db.audit()["ok"] is True
    finally:
        for worker in workers:
            worker.close()


def _transition_after_barrier(database, barrier, reservation_id, action):
    barrier.wait(timeout=30)
    try:
        return "ok", database.transition(reservation_id, action)
    except DomainError as error:
        return "error", error.code


def test_confirm_cancel_race_has_one_terminal_winner_and_one_audit(db):
    db.create_product("RACE", 3)
    held = db.reserve("RACE", 1, 60, "terminal-race")["reservation"]
    workers = _make_workers(db, 2)
    barrier = Barrier(2)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_transition_after_barrier, workers[0], barrier, held["id"], "confirm"),
                pool.submit(_transition_after_barrier, workers[1], barrier, held["id"], "cancel"),
            ]
            results = [future.result(timeout=60) for future in futures]
        successes = [value for kind, value in results if kind == "ok"]
        errors = [value for kind, value in results if kind == "error"]
        assert len(successes) == 1
        assert errors == ["invalid_transition"]
        state = db.get_reservation(held["id"])["state"]
        assert state in {"confirmed", "cancelled"}
        product = db.get_product("RACE")
        assert product["reserved"] == 0
        assert product["available"] + product["committed"] == product["total"] == 3
        assert _audit_count(db, held["id"]) == 2
        assert db.audit()["ok"] is True
    finally:
        for worker in workers:
            worker.close()


def test_duplicate_confirm_race_commits_one_transition_and_both_succeed(db):
    db.create_product("RACE", 4)
    held = db.reserve("RACE", 2, 60, "confirm-race")["reservation"]
    workers = _make_workers(db, 2)
    barrier = Barrier(2)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_transition_after_barrier, worker, barrier, held["id"], "confirm")
                for worker in workers
            ]
            results = [future.result(timeout=60) for future in futures]
        assert [kind for kind, _ in results] == ["ok", "ok"]
        assert all(value["state"] == "confirmed" for _, value in results)
        assert db.get_product("RACE") == {
            "sku": "RACE",
            "total": 4,
            "available": 2,
            "reserved": 0,
            "committed": 2,
        }
        assert _audit_count(db, held["id"]) == 2
        assert db.audit()["ok"] is True
    finally:
        for worker in workers:
            worker.close()


def _expire_after_barrier(database, barrier):
    barrier.wait(timeout=30)
    return database.expire(limit=10)


def test_simultaneous_expire_and_confirm_releases_expired_hold_once(db):
    now = [10_000]

    def clock(_connection):
        return now[0]

    creator = Database(db.engine.url, clock=clock)
    creator.create_product("RACE", 3)
    held = creator.reserve("RACE", 2, 5, "expire-confirm")["reservation"]
    now[0] = held["expires_at"]
    workers = _make_workers(db, 2, clock=clock)
    barrier = Barrier(2)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            expire_future = pool.submit(_expire_after_barrier, workers[0], barrier)
            confirm_future = pool.submit(
                _transition_after_barrier, workers[1], barrier, held["id"], "confirm"
            )
            sweep = expire_future.result(timeout=60)
            transition = confirm_future.result(timeout=60)
        assert sweep["expired"] in (0, 1)
        assert transition == ("error", "expired")
        assert creator.get_reservation(held["id"])["state"] == "expired"
        assert creator.get_product("RACE") == {
            "sku": "RACE",
            "total": 3,
            "available": 3,
            "reserved": 0,
            "committed": 0,
        }
        assert _audit_count(creator, held["id"], "expired") == 1
        assert creator.audit()["ok"] is True
    finally:
        for worker in workers:
            worker.close()
        creator.close()


def _raise_on_audit_insert(_connection, _cursor, statement, _parameters, _context, _executemany):
    normalized = statement.lower()
    if normalized.lstrip().startswith("insert") and "sh_audit_events" in normalized:
        raise RuntimeError("injected audit write failure")


def test_audit_insert_failure_rolls_back_reservation_and_transition(db):
    db.create_product("RACE", 3)
    event.listen(db.engine, "before_cursor_execute", _raise_on_audit_insert)
    try:
        with pytest.raises(RuntimeError, match="injected audit write failure"):
            db.reserve("RACE", 1, 60, "retry-create")
    finally:
        event.remove(db.engine, "before_cursor_execute", _raise_on_audit_insert)
    assert db.get_product("RACE")["available"] == 3
    with db.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(reservations)) == 0
        assert connection.scalar(select(func.count()).select_from(audit_events)) == 0

    created = db.reserve("RACE", 1, 60, "retry-create")
    assert created["replayed"] is False
    reservation_id = created["reservation"]["id"]
    event.listen(db.engine, "before_cursor_execute", _raise_on_audit_insert)
    try:
        with pytest.raises(RuntimeError, match="injected audit write failure"):
            db.transition(reservation_id, "confirm")
    finally:
        event.remove(db.engine, "before_cursor_execute", _raise_on_audit_insert)
    assert db.get_reservation(reservation_id)["state"] == "held"
    assert db.get_product("RACE")["reserved"] == 1
    assert db.get_product("RACE")["committed"] == 0
    assert _audit_count(db, reservation_id) == 1
    assert db.transition(reservation_id, "confirm")["state"] == "confirmed"
    assert _audit_count(db, reservation_id) == 2
    assert db.audit()["ok"] is True


def test_processes_allocate_only_available_stock_with_independent_engines(db):
    db.create_product("PROCESS", 2)
    database_url = str(db.engine.url)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        results = list(pool.map(_process_reserve, [database_url] * 4, [f"p-{i}" for i in range(4)]))
    successes = [result for result in results if result[0] == "ok"]
    errors = [result for result in results if result[0] == "error"]
    assert len(successes) == 2
    assert errors == [("error", "insufficient_stock")] * 2
    assert len({result[2] for result in successes}) == 2
    assert db.get_product("PROCESS") == {
        "sku": "PROCESS",
        "total": 2,
        "available": 0,
        "reserved": 2,
        "committed": 0,
    }
    assert db.audit()["ok"] is True
