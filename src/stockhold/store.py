"""SQL storage and database-enforced inventory counters."""

import re
import uuid
from collections import defaultdict

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    cast,
    create_engine,
    event,
    extract,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
products = Table(
    "sh_products",
    metadata,
    Column("sku", String(48), primary_key=True),
    Column("total", Integer, nullable=False),
    Column("available", Integer, nullable=False),
    Column("reserved", Integer, nullable=False),
    Column("committed", Integer, nullable=False),
    CheckConstraint("total BETWEEN 1 AND 1000000", name="total_range"),
    CheckConstraint("available >= 0 AND reserved >= 0 AND committed >= 0", name="nonnegative"),
    CheckConstraint("available + reserved + committed = total", name="stock_conservation"),
    CheckConstraint(
        "total = CAST(total AS INTEGER) AND available = CAST(available AS INTEGER) "
        "AND reserved = CAST(reserved AS INTEGER) "
        "AND committed = CAST(committed AS INTEGER)",
        name="integer_counts",
    ),
)
schema_version = Table(
    "sh_schema",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
)
reservations = Table(
    "sh_reservations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("sku", String(48), ForeignKey("sh_products.sku"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("ttl_seconds", Integer, nullable=False),
    Column("idempotency_key", String(64), nullable=False),
    Column("state", String(10), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("sku", "idempotency_key", name="uq_sh_reservations_sku_key"),
    CheckConstraint("quantity BETWEEN 1 AND 1000000", name="reservation_quantity_range"),
    CheckConstraint("ttl_seconds BETWEEN 1 AND 3600", name="reservation_ttl_range"),
    CheckConstraint(
        "state IN ('held', 'confirmed', 'cancelled', 'expired')", name="reservation_state"
    ),
)
Index(
    "ix_sh_reservations_expiry",
    reservations.c.state,
    reservations.c.expires_at,
    reservations.c.id,
)
audit_events = Table(
    "sh_audit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reservation_id", String(36), ForeignKey("sh_reservations.id"), nullable=False),
    Column("sku", String(48), nullable=False),
    Column("from_state", String(10)),
    Column("to_state", String(10), nullable=False),
    Column("occurred_at", BigInteger, nullable=False),
)


def validate_sku(sku):
    if not isinstance(sku, str) or re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,47}", sku) is None:
        raise ValueError("SKU must be 1-48 uppercase letters, digits, underscores or hyphens")


def validate_quantity(quantity):
    if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
        raise ValueError("Quantity must be an integer between 1 and 1000000")


class DomainError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Database:
    def __init__(self, url, clock=None):
        parsed = make_url(url)
        if parsed.drivername == "sqlite":
            if not parsed.database or parsed.database == ":memory:" or parsed.query:
                raise ValueError("Use a file-backed SQLite database without URI query options")
            connect_args = {"check_same_thread": False, "timeout": 5}
        elif parsed.drivername == "postgresql+psycopg":
            connect_args = {
                "connect_timeout": 5,
                "options": "-c lock_timeout=5000 -c statement_timeout=10000",
            }
        else:
            raise ValueError("Supported databases are sqlite and postgresql+psycopg")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.clock = clock
        self.engine = create_engine(parsed, connect_args=connect_args, pool_pre_ping=True)
        if parsed.drivername == "sqlite":

            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, record):
                connection.execute("PRAGMA foreign_keys=ON")

        try:
            with self.engine.begin() as conn:
                tables = inspect(conn).get_table_names()
                has_version = "sh_schema" in tables
                if has_version:
                    versions = conn.execute(select(schema_version)).all()
                    if len(versions) != 1 or versions[0].id != 1 or versions[0].version != 1:
                        raise ValueError("Unsupported database schema version")
                elif any(name.startswith("sh_") for name in tables):
                    raise ValueError("Unversioned database schema; explicit migration required")
                metadata.create_all(conn)
                if not has_version:
                    conn.execute(insert(schema_version).values(id=1, version=1))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.engine.dispose()

    def ping(self):
        with self.engine.connect() as conn:
            return (
                conn.scalar(select(schema_version.c.version).where(schema_version.c.id == 1)) == 1
            )

    def create_product(self, sku, quantity):
        validate_sku(sku)
        validate_quantity(quantity)
        values = {
            "sku": sku,
            "total": quantity,
            "available": quantity,
            "reserved": 0,
            "committed": 0,
        }
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(products).values(**values))
        except IntegrityError as error:
            raise DomainError("sku_exists", "SKU already exists") from error
        return values

    def get_product(self, sku):
        validate_sku(sku)
        with self.engine.connect() as conn:
            result = conn.execute(select(products).where(products.c.sku == sku)).mappings().first()
        if result is None:
            raise DomainError("not_found", "Product not found", 404)
        return dict(result)

    def _lock_product(self, conn, sku):
        result = conn.execute(
            update(products).where(products.c.sku == sku).values(available=products.c.available)
        )
        if result.rowcount != 1:
            raise DomainError("not_found", "Product not found", 404)

    def _lock_reservation_product(self, conn, reservation_id):
        reservation_sku = (
            select(reservations.c.sku).where(reservations.c.id == reservation_id).scalar_subquery()
        )
        result = conn.execute(
            update(products)
            .where(products.c.sku == reservation_sku)
            .values(available=products.c.available)
        )
        if result.rowcount != 1:
            raise DomainError("not_found", "Reservation not found", 404)

    def _now(self, conn):
        if self.clock is not None:
            value = self.clock(conn)
        elif self.engine.dialect.name == "sqlite":
            value = conn.execute(text("SELECT CAST(strftime('%s','now') AS INTEGER)")).scalar_one()
        else:
            value = conn.execute(
                select(cast(func.floor(extract("epoch", func.clock_timestamp())), BigInteger))
            ).scalar_one()
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("database clock must return an integer epoch second")
        return value

    @staticmethod
    def _public(row):
        return {
            "id": row["id"],
            "sku": row["sku"],
            "quantity": row["quantity"],
            "ttl_seconds": row["ttl_seconds"],
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "updated_at": row["updated_at"],
        }

    def _finish(self, conn, reservation, state, now):
        sku = reservation["sku"]
        quantity = reservation["quantity"]
        if state == "confirmed":
            conn.execute(
                update(products)
                .where(products.c.sku == sku)
                .values(
                    reserved=products.c.reserved - quantity,
                    committed=products.c.committed + quantity,
                )
            )
        else:
            conn.execute(
                update(products)
                .where(products.c.sku == sku)
                .values(
                    reserved=products.c.reserved - quantity,
                    available=products.c.available + quantity,
                )
            )
        conn.execute(
            update(reservations)
            .where(reservations.c.id == reservation["id"], reservations.c.state == "held")
            .values(state=state, updated_at=now)
        )
        conn.execute(
            insert(audit_events).values(
                reservation_id=reservation["id"],
                sku=sku,
                from_state="held",
                to_state=state,
                occurred_at=now,
            )
        )
        updated = (
            conn.execute(select(reservations).where(reservations.c.id == reservation["id"]))
            .mappings()
            .one()
        )
        return self._public(updated)

    def reserve(self, sku, quantity, ttl_seconds, key):
        validate_sku(sku)
        validate_quantity(quantity)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError("TTL must be an integer between 1 and 3600 seconds")
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key, re.ASCII) is None:
            raise ValueError(
                "Idempotency key must be 1-64 ASCII letters, digits, underscores or hyphens"
            )

        with self.engine.begin() as conn:
            self._lock_product(conn, sku)
            now = self._now(conn)
            existing = (
                conn.execute(
                    select(reservations).where(
                        reservations.c.sku == sku,
                        reservations.c.idempotency_key == key,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["quantity"] != quantity or existing["ttl_seconds"] != ttl_seconds:
                    raise DomainError(
                        "idempotency_conflict", "Idempotency key was used for a different request"
                    )
                if existing["state"] == "held" and existing["expires_at"] <= now:
                    current = self._finish(conn, existing, "expired", now)
                else:
                    current = self._public(existing)
                return {"reservation": current, "replayed": True}

            product = conn.execute(select(products).where(products.c.sku == sku)).mappings().one()
            if product["available"] < quantity:
                raise DomainError("insufficient_stock", "Insufficient available stock")
            identifier = str(uuid.uuid4())
            values = {
                "id": identifier,
                "sku": sku,
                "quantity": quantity,
                "ttl_seconds": ttl_seconds,
                "idempotency_key": key,
                "state": "held",
                "created_at": now,
                "expires_at": now + ttl_seconds,
                "updated_at": now,
            }
            conn.execute(insert(reservations).values(**values))
            conn.execute(
                update(products)
                .where(products.c.sku == sku)
                .values(
                    available=products.c.available - quantity,
                    reserved=products.c.reserved + quantity,
                )
            )
            conn.execute(
                insert(audit_events).values(
                    reservation_id=identifier,
                    sku=sku,
                    from_state=None,
                    to_state="held",
                    occurred_at=now,
                )
            )
            reservation = self._public(values)
        return {"reservation": reservation, "replayed": False}

    def get_reservation(self, reservation_id):
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(reservations).where(reservations.c.id == reservation_id))
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError("not_found", "Reservation not found", 404)
        return self._public(row)

    def transition(self, reservation_id, action):
        if action not in {"confirm", "cancel"}:
            raise DomainError("invalid_transition", "Action must be confirm or cancel")
        deferred_error = None
        with self.engine.begin() as conn:
            self._lock_reservation_product(conn, reservation_id)
            now = self._now(conn)
            reservation = (
                conn.execute(select(reservations).where(reservations.c.id == reservation_id))
                .mappings()
                .one()
            )
            if reservation["state"] == "held" and reservation["expires_at"] <= now:
                result = self._finish(conn, reservation, "expired", now)
                deferred_error = DomainError("expired", "Reservation has expired")
            elif reservation["state"] == "expired":
                result = self._public(reservation)
                deferred_error = DomainError("expired", "Reservation has expired")
            else:
                target = "confirmed" if action == "confirm" else "cancelled"
                if reservation["state"] == target:
                    result = self._public(reservation)
                elif reservation["state"] != "held":
                    raise DomainError("invalid_transition", "Reservation is already terminal")
                else:
                    result = self._finish(conn, reservation, target, now)
        if deferred_error is not None:
            raise deferred_error
        return result

    def expire(self, limit=100):
        """Process at most limit candidates, committing each release independently."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Sweep limit must be an integer between 1 and 1000")
        with self.engine.connect() as conn:
            cutoff = self._now(conn)
            candidates = (
                conn.execute(
                    select(reservations.c.id)
                    .where(reservations.c.state == "held", reservations.c.expires_at <= cutoff)
                    .order_by(reservations.c.expires_at, reservations.c.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        expired, released = 0, 0
        for identifier in candidates:
            with self.engine.begin() as conn:
                self._lock_reservation_product(conn, identifier)
                now = self._now(conn)
                held = (
                    conn.execute(select(reservations).where(reservations.c.id == identifier))
                    .mappings()
                    .one()
                )
                if held["state"] == "held" and held["expires_at"] <= now:
                    self._finish(conn, held, "expired", now)
                    expired += 1
                    released += held["quantity"]
        return {"examined": len(candidates), "expired": expired, "released_units": released}

    def audit(self):
        """Compare counters and transition history from one consistent read snapshot."""
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "postgresql":
                conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                if self.engine.dialect.name == "sqlite":
                    # sqlite3 legacy mode doesn't BEGIN on SELECT. Start a real read snapshot.
                    conn.exec_driver_sql("BEGIN")
                stock = conn.execute(select(products)).mappings().all()
                holds = conn.execute(select(reservations)).mappings().all()
                events = (
                    conn.execute(select(audit_events).order_by(audit_events.c.id)).mappings().all()
                )
        totals = defaultdict(lambda: {"held": 0, "confirmed": 0})
        histories = defaultdict(list)
        for event_row in events:
            histories[event_row["reservation_id"]].append(event_row)
        violations = []
        for held in holds:
            if held["state"] in {"held", "confirmed"}:
                totals[held["sku"]][held["state"]] += held["quantity"]
            expected = [(None, "held", held["created_at"])]
            if held["state"] != "held":
                expected.append(("held", held["state"], held["updated_at"]))
            history = histories[held["id"]]
            actual = [
                (item["from_state"], item["to_state"], item["occurred_at"]) for item in history
            ]
            if actual != expected or any(item["sku"] != held["sku"] for item in history):
                violations.append({"reservation_id": held["id"], "error": "history_mismatch"})
        for item in stock:
            reserved = totals[item["sku"]]["held"]
            committed = totals[item["sku"]]["confirmed"]
            if (
                item["reserved"] != reserved
                or item["committed"] != committed
                or item["available"] != item["total"] - reserved - committed
            ):
                violations.append({"sku": item["sku"], "error": "counter_mismatch"})
        return {
            "ok": not violations,
            "products_checked": len(stock),
            "reservations_checked": len(holds),
            "audit_events_checked": len(events),
            "violations": violations,
        }
