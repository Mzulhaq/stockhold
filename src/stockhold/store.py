"""SQL storage and database-enforced inventory counters."""

import re

from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    insert,
    inspect,
    select,
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
    def __init__(self, url):
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
