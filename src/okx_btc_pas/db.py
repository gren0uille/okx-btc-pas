# Общие объекты базы: реестр таблиц, схемы слоёв и миграции структуры
#
# Слои хранения:
#   raw   — данные источников в исходном виде
#   clean — проверенные и типизированные данные
#   mart  — витрины для моделей и дашбордов
#   hist  — история изменений значимых сущностей
#   meta  — метаданные: каталог наборов, происхождение, журналы запусков

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, inspect, text

SCHEMAS = ("raw", "clean", "mart", "hist", "meta")
metadata = MetaData()

schema_migration = Table(
    "schema_migration", metadata,
    Column("version", Integer, primary_key=True),
    Column("description", String(200), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
    schema="meta",
)

# Изменения структуры уже существующих таблиц. Новые таблицы создаёт
# create_all, а он не добавляет столбцы в таблицы, созданные раньше.
# Каждая миграция применяется один раз и фиксируется в meta.schema_migration
MIGRATIONS = [
    (1, "load_log: число обновлённых строк",
     [("load_log", None, "rows_updated", "INTEGER NOT NULL DEFAULT 0")]),
    (2, "load_log: идентификатор запуска конвейера",
     [("load_log", None, "run_id", "VARCHAR(40)")]),
    (3, "data_quality_log: запуск, критичность и действие системы",
     [("data_quality_log", None, "run_id", "VARCHAR(40)"),
      ("data_quality_log", None, "severity", "VARCHAR(20) NOT NULL DEFAULT 'error'"),
      ("data_quality_log", None, "action", "VARCHAR(30) NOT NULL DEFAULT 'reject_row'")]),
    (4, "daily_market_mart: индекс страха и жадности, ставка ФРС",
     [("daily_market_mart", "mart", "fear_greed", "INTEGER"),
      ("daily_market_mart", "mart", "fear_greed_age_days", "INTEGER"),
      ("daily_market_mart", "mart", "fed_rate", "NUMERIC(10, 4)"),
      ("daily_market_mart", "mart", "fed_rate_age_days", "INTEGER")]),
]


# Создаём схемы, недостающие таблицы и применяем миграции
def initialize_database(engine):
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            for schema in SCHEMAS:
                conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    metadata.create_all(engine)
    apply_migrations(engine)


def _qualified(conn, schema, table):
    if schema and conn.dialect.name == "postgresql":
        return f"{schema}.{table}"
    return table


# Добавляем столбцы, которых нет в существующих таблицах
# Наличие столбца проверяется по фактической структуре базы, поэтому
# на свежей базе миграция ничего не меняет и только фиксируется
def apply_migrations(engine):
    with engine.begin() as conn:
        done = {row[0] for row in conn.execute(schema_migration.select())}
        for version, description, changes in MIGRATIONS:
            if version in done:
                continue
            inspector = inspect(conn)
            for table, schema, column, ddl in changes:
                if not inspector.has_table(table, schema=schema):
                    continue
                existing = {c["name"] for c in inspector.get_columns(table, schema=schema)}
                if column not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {_qualified(conn, schema, table)} "
                        f"ADD COLUMN {column} {ddl}"))
            conn.execute(schema_migration.insert().values(
                version=version, description=description,
                applied_at=datetime.now(timezone.utc)))
