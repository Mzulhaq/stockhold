import os

import pytest

from stockhold.store import Database, metadata


@pytest.fixture
def db(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    database = Database(url)
    if os.environ.get("TEST_DATABASE_URL"):
        metadata.drop_all(database.engine)
        database.close()
        database = Database(url)
    yield database
    if os.environ.get("TEST_DATABASE_URL"):
        metadata.drop_all(database.engine)
    database.close()
