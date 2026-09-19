"""Shared test database.

PostgreSQL keeps raw, clean and mart in separate schemas, so two tables may
share a name. SQLite has no schemas, but ATTACH gives the same separation in
memory, which lets the tests exercise the real table definitions unchanged.
"""

import pytest
from sqlalchemy import create_engine, event

from okx_btc_pas.ingestion import initialize_database

SCHEMAS = ("raw", "clean", "mart")


@pytest.fixture
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def attach_schemas(connection, record):
        for schema in SCHEMAS:
            connection.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    initialize_database(engine)
    yield engine
    engine.dispose()
