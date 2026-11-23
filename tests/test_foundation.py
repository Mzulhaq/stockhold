import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from stockhold.api import create_app
from stockhold.store import Database, DomainError, products, schema_version


def test_create_read_and_persist(db):
    expected = {"sku": "WIDGET-1", "total": 7, "available": 7, "reserved": 0, "committed": 0}
    assert db.create_product("WIDGET-1", 7) == expected
    assert db.get_product("WIDGET-1") == expected
    reopened = Database(db.engine.url)
    try:
        assert reopened.get_product("WIDGET-1") == expected
    finally:
        reopened.close()


def test_duplicate_and_missing_product(db):
    db.create_product("ONE", 1)
    with pytest.raises(DomainError, match="exists"):
        db.create_product("ONE", 10)
    assert db.get_product("ONE")["total"] == 1
    with pytest.raises(DomainError) as error:
        db.get_product("MISSING")
    assert error.value.status == 404


@pytest.mark.parametrize("quantity", [0, -1, 1000001, True, "3", 1.5])
def test_invalid_quantities(db, quantity):
    with pytest.raises(ValueError):
        db.create_product("VALID", quantity)


@pytest.mark.parametrize("sku", ["", "lowercase", "A B", "a/b", "X" * 49, None])
def test_invalid_skus(db, sku):
    with pytest.raises(ValueError):
        db.create_product(sku, 1)


def test_database_constraints_prevent_negative_or_unbalanced_stock(db):
    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(
            insert(products).values(sku="BAD", total=1, available=-1, reserved=2, committed=0)
        )
    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(
            insert(products).values(sku="BAD", total=1, available=2, reserved=0, committed=0)
        )
    assert db.ping()


def test_unknown_schema_version_fails_closed(tmp_path):
    url = f"sqlite:///{tmp_path / 'future.db'}"
    database = Database(url)
    with database.engine.begin() as conn:
        conn.execute(update(schema_version).values(version=99))
    database.close()
    with pytest.raises(ValueError, match="schema"):
        Database(url)


def test_http_create_health_read_and_conflict(db):
    with TestClient(create_app(db)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        created = client.post("/v1/products", json={"sku": "HTTP-1", "quantity": 5})
        assert created.status_code == 201
        assert client.get("/v1/products/HTTP-1").json() == created.json()
        assert client.post("/v1/products", json={"sku": "HTTP-1", "quantity": 5}).status_code == 409
        assert client.get("/v1/products/MISSING").status_code == 404
        assert (
            client.post("/v1/products", json={"sku": "HTTP-2", "quantity": True}).status_code == 422
        )
        assert (
            client.post("/v1/products", json={"sku": "HTTP-2", "quantity": 1, "x": 1}).status_code
            == 422
        )


@pytest.mark.parametrize(
    "raw", [b"{", b'{"sku":"A","sku":"B","quantity":1}', b'{"quantity":NaN}', b"\xff"]
)
def test_malformed_json(db, raw):
    with TestClient(create_app(db)) as client:
        response = client.post(
            "/v1/products", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400


def test_stream_limit_and_media_type(db):
    with TestClient(create_app(db)) as client:
        assert client.post("/v1/products", content="{}").status_code == 415
        chunks = iter([b" " * 4096, b"{}"])
        response = client.post(
            "/v1/products", content=chunks, headers={"Content-Type": "application/json"}
        )
        assert "content-length" not in response.request.headers
        assert response.status_code == 413
