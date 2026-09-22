# Общая тестовая база
# В PostgreSQL raw, clean и mart лежат в разных схемах, поэтому две таблицы
# могут называться одинаково. В SQLite схем нет, но ATTACH даёт такое же
# разделение в памяти — тесты работают с настоящими определениями таблиц

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
