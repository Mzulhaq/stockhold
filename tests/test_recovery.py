import json

import pytest
from sqlalchemy import update

from stockhold.cli import main
from stockhold.store import Database, audit_events, products


def test_bounded_expiry_sweep_releases_each_hold_once(db):
    now = [1000]
    db.clock = lambda conn: now[0]
    db.create_product("SWEEP", 6)
    holds = [db.reserve("SWEEP", 2, 10, f"hold-{i}")["reservation"] for i in range(3)]
    assert db.expire(2) == {"examined": 0, "expired": 0, "released_units": 0}
    now[0] = 1010
    assert db.get_reservation(holds[0]["id"])["state"] == "held"
    assert db.expire(2) == {"examined": 2, "expired": 2, "released_units": 4}
    assert db.get_product("SWEEP")["available"] == 4
    assert db.expire(2) == {"examined": 1, "expired": 1, "released_units": 2}
    assert db.expire(2) == {"examined": 0, "expired": 0, "released_units": 0}
    assert db.audit() == {
        "ok": True,
        "products_checked": 1,
        "reservations_checked": 3,
        "audit_events_checked": 6,
        "violations": [],
    }


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "100", 1.5])
def test_invalid_sweep_limit_is_rejected(db, limit):
    with pytest.raises(ValueError):
        db.expire(limit)


def test_audit_detects_cross_table_drift_even_when_product_equation_holds(db):
    db.create_product("DRIFT", 5)
    db.reserve("DRIFT", 2, 300, "one")
    assert db.audit()["ok"] is True
    with db.engine.begin() as conn:
        conn.execute(
            update(products).where(products.c.sku == "DRIFT").values(available=4, reserved=1)
        )
    result = db.audit()
    assert result["ok"] is False
    assert any(
        v["error"] == "counter_mismatch" and v["sku"] == "DRIFT" for v in result["violations"]
    )


def test_audit_detects_invalid_history(db):
    db.create_product("HISTORY", 5)
    held = db.reserve("HISTORY", 2, 300, "one")["reservation"]
    db.transition(held["id"], "confirm")
    with db.engine.begin() as conn:
        conn.execute(
            update(audit_events)
            .where(audit_events.c.from_state == "held")
            .values(to_state="cancelled")
        )
    result = db.audit()
    assert result["ok"] is False
    assert any(v["error"] == "history_mismatch" for v in result["violations"])


def test_epoch_after_2038_is_supported(db):
    db.clock = lambda conn: 2_200_000_000
    db.create_product("FUTURE", 1)
    held = db.reserve("FUTURE", 1, 3600, "later")["reservation"]
    assert held["expires_at"] == 2_200_003_600
    assert db.transition(held["id"], "confirm")["state"] == "confirmed"
    assert db.audit()["ok"] is True


def test_recovery_cli_and_failing_audit_exit_status(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    database = Database(url, clock=lambda conn: 1000)
    database.create_product("CLI", 2)
    database.reserve("CLI", 1, 1, "past")
    assert main(["expire", "--database-url", url, "--limit", "10"]) == 0
    assert json.loads(capsys.readouterr().out)["released_units"] == 1
    assert main(["audit", "--database-url", url]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    with database.engine.begin() as conn:
        conn.execute(update(products).values(available=1, reserved=1))
    assert main(["audit", "--database-url", url]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    database.close()
