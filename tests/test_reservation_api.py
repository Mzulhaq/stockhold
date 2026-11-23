import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from stockhold.api import create_app


@pytest.fixture
def client(db):
    db.create_product("API", 3)
    with TestClient(create_app(db)) as client:
        yield client


def test_hold_replay_confirmation_and_conflicting_cancel(client):
    payload = {"sku": "API", "quantity": 2, "ttl_seconds": 300}
    first = client.post("/v1/reservations", json=payload, headers={"Idempotency-Key": "order-1"})
    assert first.status_code == 201
    assert first.json()["replayed"] is False
    reservation = first.json()["reservation"]
    assert reservation["state"] == "held"
    assert "idempotency_key" not in reservation
    path = "/v1/reservations/" + reservation["id"]
    assert client.get(path).json() == reservation
    replay = client.post("/v1/reservations", json=payload, headers={"Idempotency-Key": "order-1"})
    assert replay.status_code == 200
    assert replay.json() == {"reservation": reservation, "replayed": True}
    assert client.get("/v1/products/API").json()["available"] == 1
    confirmed = client.post(path + "/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "confirmed"
    assert client.post(path + "/confirm").json() == confirmed.json()
    assert client.post(path + "/cancel").status_code == 409
    assert client.get("/v1/products/API").json() == {
        "sku": "API",
        "total": 3,
        "available": 1,
        "reserved": 0,
        "committed": 2,
    }


def test_cancel_returns_stock_and_defaults_ttl(client):
    response = client.post(
        "/v1/reservations",
        json={"sku": "API", "quantity": 3},
        headers={"Idempotency-Key": "cancel-me"},
    )
    reservation = response.json()["reservation"]
    assert reservation["ttl_seconds"] == 300
    path = "/v1/reservations/" + reservation["id"] + "/cancel"
    assert client.post(path).json()["state"] == "cancelled"
    assert client.post(path).status_code == 200
    assert client.get("/v1/products/API").json()["available"] == 3


@pytest.mark.parametrize(
    "payload,key",
    [
        ({"sku": "API", "quantity": 1}, None),
        ({"sku": "API", "quantity": 1}, "bad key"),
        ({"sku": "API", "quantity": True}, "key"),
        ({"sku": "API", "quantity": 1, "ttl_seconds": 0}, "key"),
        ({"sku": "API", "quantity": 1, "ttl_seconds": True}, "key"),
        ({"sku": "API", "quantity": 1, "ttl_seconds": 3601}, "key"),
        ({"sku": "API", "quantity": 1, "extra": 2}, "key"),
    ],
)
def test_invalid_hold_inputs_do_not_allocate(client, payload, key):
    headers = {} if key is None else {"Idempotency-Key": key}
    assert client.post("/v1/reservations", json=payload, headers=headers).status_code == 422
    assert client.get("/v1/products/API").json()["reserved"] == 0


def test_insufficient_stock_conflict_and_unknown_reservation(client):
    response = client.post(
        "/v1/reservations",
        json={"sku": "API", "quantity": 4},
        headers={"Idempotency-Key": "too-many"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "insufficient_stock"
    assert client.get("/v1/reservations/unknown").status_code == 404
    assert client.post("/v1/reservations/unknown/confirm").status_code == 404


def test_action_body_is_bounded_and_must_be_empty(client):
    assert client.post("/v1/reservations/unknown/cancel", content=b"x" * 4097).status_code == 413
    assert client.post("/v1/reservations/unknown/cancel", json={"quantity": 1}).status_code == 422


def test_expired_http_confirmation_releases_stock_before_error(db):
    now = [1000]
    db.clock = lambda conn: now[0]
    db.create_product("TTL", 2)
    with TestClient(create_app(db)) as client:
        held = client.post(
            "/v1/reservations",
            json={"sku": "TTL", "quantity": 2, "ttl_seconds": 1},
            headers={"Idempotency-Key": "timeout"},
        ).json()["reservation"]
        now[0] = 1001
        path = "/v1/reservations/" + held["id"]
        response = client.post(path + "/confirm")
        assert response.status_code == 409
        assert response.json()["error"] == "expired"
        assert client.get(path).json()["state"] == "expired"
        assert client.get("/v1/products/TTL").json()["available"] == 2


def test_database_failure_is_sanitized(db, monkeypatch):
    def fail(sku):
        raise OperationalError("SELECT private_value", {}, Exception("secret-connection-string"))

    monkeypatch.setattr(db, "get_product", fail)
    with TestClient(create_app(db)) as client:
        response = client.get("/v1/products/ANY")
        assert response.status_code == 503
        assert response.json() == {
            "error": "database_unavailable",
            "detail": "Database unavailable",
        }
