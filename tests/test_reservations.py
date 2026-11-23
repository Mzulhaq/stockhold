from __future__ import annotations

import pytest
from sqlalchemy import event, inspect, text

from stockhold.store import Database, DomainError


@pytest.fixture
def clocked_db(db):
    now = [1000]
    database = Database(db.engine.url, clock=lambda _connection: now[0])
    yield database, now
    database.close()


def audit_rows(database, reservation_id):
    with database.engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT reservation_id, sku, from_state, to_state, occurred_at "
                    "FROM sh_audit_events WHERE reservation_id = :reservation_id ORDER BY id"
                ),
                {"reservation_id": reservation_id},
            )
            .mappings()
            .all()
        )


def test_reservation_lifecycle_conserves_inventory_and_audits_first_transitions(clocked_db):
    database, _now = clocked_db
    database.create_product("WIDGET", 10)

    created = database.reserve("WIDGET", 4, 60, "order_1")
    reservation = created["reservation"]
    assert created["replayed"] is False
    assert reservation == {
        "id": reservation["id"],
        "sku": "WIDGET",
        "quantity": 4,
        "ttl_seconds": 60,
        "state": "held",
        "created_at": 1000,
        "expires_at": 1060,
        "updated_at": 1000,
    }
    assert set(reservation) == {
        "id",
        "sku",
        "quantity",
        "ttl_seconds",
        "state",
        "created_at",
        "expires_at",
        "updated_at",
    }
    assert database.get_product("WIDGET") == {
        "sku": "WIDGET",
        "total": 10,
        "available": 6,
        "reserved": 4,
        "committed": 0,
    }

    confirmed = database.transition(reservation["id"], "confirm")
    assert confirmed["state"] == "confirmed"
    assert database.transition(reservation["id"], "confirm") == confirmed
    with pytest.raises(DomainError) as error:
        database.transition(reservation["id"], "cancel")
    assert error.value.code == "invalid_transition"
    assert database.get_product("WIDGET") == {
        "sku": "WIDGET",
        "total": 10,
        "available": 6,
        "reserved": 0,
        "committed": 4,
    }
    rows = audit_rows(database, reservation["id"])
    assert [(row["from_state"], row["to_state"], row["occurred_at"]) for row in rows] == [
        (None, "held", 1000),
        ("held", "confirmed", 1000),
    ]


def test_same_key_replays_same_current_reservation_state(clocked_db):
    database, _now = clocked_db
    database.create_product("ONE", 5)
    first = database.reserve("ONE", 2, 30, "same-key")
    database.transition(first["reservation"]["id"], "cancel")
    replay = database.reserve("ONE", 2, 30, "same-key")
    assert replay["replayed"] is True
    assert replay["reservation"]["id"] == first["reservation"]["id"]
    assert replay["reservation"]["state"] == "cancelled"
    assert database.get_product("ONE")["available"] == 5
    assert len(audit_rows(database, first["reservation"]["id"])) == 2


@pytest.mark.parametrize("quantity,ttl", [(3, 60), (2, 61)])
def test_reusing_key_with_changed_request_conflicts_without_side_effects(clocked_db, quantity, ttl):
    database, _now = clocked_db
    database.create_product("ONE", 5)
    first = database.reserve("ONE", 2, 60, "stable")
    with pytest.raises(DomainError) as error:
        database.reserve("ONE", quantity, ttl, "stable")
    assert error.value.code == "idempotency_conflict"
    assert database.get_product("ONE")["available"] == 3
    assert database.get_product("ONE")["reserved"] == 2
    assert len(audit_rows(database, first["reservation"]["id"])) == 1


def test_insufficient_stock_does_not_create_reservation_or_audit(clocked_db):
    database, _now = clocked_db
    database.create_product("ONE", 1)
    with pytest.raises(DomainError) as error:
        database.reserve("ONE", 2, 10, "too-many")
    assert error.value.code == "insufficient_stock"
    assert database.get_product("ONE")["available"] == 1
    assert inspect(database.engine).has_table("sh_reservations")
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM sh_reservations")) == 0
        assert connection.scalar(text("SELECT count(*) FROM sh_audit_events")) == 0


def test_reservations_include_expiry_sweep_index(clocked_db):
    database, _now = clocked_db
    indexes = inspect(database.engine).get_indexes("sh_reservations")
    assert any(index["column_names"] == ["state", "expires_at", "id"] for index in indexes)


def test_transition_locks_product_before_reading_reservation(clocked_db):
    database, _now = clocked_db
    database.create_product("ONE", 2)
    held = database.reserve("ONE", 1, 30, "lock-order")["reservation"]
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().upper())

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        database.transition(held["id"], "confirm")
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert statements[0].startswith("UPDATE SH_PRODUCTS")


def test_expiry_at_exact_boundary_commits_release_before_conflict(clocked_db):
    database, now = clocked_db
    database.create_product("ONE", 3)
    held = database.reserve("ONE", 2, 10, "expires")["reservation"]
    now[0] = held["expires_at"]
    with pytest.raises(DomainError) as error:
        database.transition(held["id"], "confirm")
    assert error.value.code == "expired"
    assert database.get_reservation(held["id"])["state"] == "expired"
    assert database.get_product("ONE") == {
        "sku": "ONE",
        "total": 3,
        "available": 3,
        "reserved": 0,
        "committed": 0,
    }
    assert [row["to_state"] for row in audit_rows(database, held["id"])] == ["held", "expired"]
    with pytest.raises(DomainError) as repeat:
        database.transition(held["id"], "confirm")
    assert repeat.value.code == "expired"
    assert len(audit_rows(database, held["id"])) == 2


def test_replay_of_expired_hold_persists_expiry_without_new_allocation(clocked_db):
    database, now = clocked_db
    database.create_product("ONE", 3)
    original = database.reserve("ONE", 2, 10, "expired-key")["reservation"]
    now[0] = original["expires_at"]
    replay = database.reserve("ONE", 2, 10, "expired-key")
    assert replay["replayed"] is True
    assert replay["reservation"]["id"] == original["id"]
    assert replay["reservation"]["state"] == "expired"
    assert database.get_product("ONE")["available"] == 3
    assert database.get_product("ONE")["reserved"] == 0
    assert len(audit_rows(database, original["id"])) == 2


def test_cancel_returns_stock_and_repeated_cancel_is_idempotent(clocked_db):
    database, _now = clocked_db
    database.create_product("ONE", 8)
    held = database.reserve("ONE", 3, 60, "cancel-me")["reservation"]
    cancelled = database.transition(held["id"], "cancel")
    assert cancelled["state"] == "cancelled"
    assert database.transition(held["id"], "cancel") == cancelled
    assert database.get_product("ONE") == {
        "sku": "ONE",
        "total": 8,
        "available": 8,
        "reserved": 0,
        "committed": 0,
    }
    assert len(audit_rows(database, held["id"])) == 2


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, 1.5, "60"])
def test_ttl_must_be_strict_integer_in_range(clocked_db, ttl):
    database, _now = clocked_db
    database.create_product("ONE", 2)
    with pytest.raises(ValueError):
        database.reserve("ONE", 1, ttl, "valid")
    assert database.get_product("ONE")["available"] == 2


@pytest.mark.parametrize("key", ["", "has space", "slash/", "x" * 65, "é", None])
def test_idempotency_key_must_be_ascii_and_bounded(clocked_db, key):
    database, _now = clocked_db
    database.create_product("ONE", 2)
    with pytest.raises(ValueError):
        database.reserve("ONE", 1, 60, key)
    assert database.get_product("ONE")["available"] == 2


def test_ttl_and_key_boundaries_are_accepted_and_idempotency_is_sku_scoped(clocked_db):
    database, _now = clocked_db
    database.create_product("ONE", 2)
    database.create_product("TWO", 2)
    key = "A" * 64
    first = database.reserve("ONE", 1, 3600, key)
    second = database.reserve("TWO", 1, 1, key)
    assert first["reservation"]["ttl_seconds"] == 3600
    assert second["reservation"]["ttl_seconds"] == 1
    assert first["reservation"]["id"] != second["reservation"]["id"]


def test_reservations_and_audit_rows_survive_reopen(clocked_db):
    database, _now = clocked_db
    database.create_product("ONE", 4)
    held = database.reserve("ONE", 2, 50, "persistent")["reservation"]
    url = database.engine.url
    database.close()
    reopened = Database(url, clock=lambda _connection: 1000)
    try:
        assert reopened.get_reservation(held["id"]) == held
        assert reopened.get_product("ONE")["reserved"] == 2
        assert len(audit_rows(reopened, held["id"])) == 1
    finally:
        reopened.close()


def test_missing_products_reservations_and_invalid_actions_are_domain_errors(clocked_db):
    database, _now = clocked_db
    with pytest.raises(DomainError) as no_product:
        database.reserve("MISSING", 1, 10, "key")
    assert no_product.value.code == "not_found"
    with pytest.raises(DomainError) as no_reservation:
        database.get_reservation("missing")
    assert no_reservation.value.code == "not_found"
    database.create_product("ONE", 1)
    held = database.reserve("ONE", 1, 10, "key")["reservation"]
    with pytest.raises(DomainError) as invalid:
        database.transition(held["id"], "expire")
    assert invalid.value.code == "invalid_transition"
