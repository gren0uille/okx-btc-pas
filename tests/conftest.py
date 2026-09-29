# Общая тестовая база
# В PostgreSQL слои лежат в разных схемах, поэтому две таблицы могут
# называться одинаково. В SQLite схем нет, но ATTACH даёт такое же
# разделение в памяти — тесты работают с настоящими определениями таблиц

import pytest
from sqlalchemy import create_engine, event

import okx_btc_pas.cleaning  # noqa: F401  регистрирует таблицы слоя clean
import okx_btc_pas.history  # noqa: F401
import okx_btc_pas.mart  # noqa: F401
from okx_btc_pas.db import SCHEMAS, initialize_database


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
