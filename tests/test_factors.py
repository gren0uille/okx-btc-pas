from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event, func, inspect, select, text

from okx_btc_pas.cleaning import build_clean, clean_fred, data_quality_log
from okx_btc_pas.db import SCHEMAS, initialize_database
from okx_btc_pas.history import external_factor_version, value_as_of
from okx_btc_pas.ingestion import load_log, raw_fng, raw_fred, raw_okx, run_loader
from okx_btc_pas.mart import build_mart, daily_mart, fed_available_from

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
END = date(2018, 1, 12)


class FakeResponse:
    def __init__(self, data=None, text_body=""):
        self.data = data
        self.text = text_body

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FactorSession:
    # Отдаёт индекс страха за 10–12 января и ставку ФРС за те же дни
    def __init__(self):
        self.fed = {10: "1.42", 11: "1.42", 12: "1.43"}
        self.fng = {10: "30", 11: "35", 12: "40"}

    def get(self, url, params, timeout):
        if "alternative.me" in url:
            data = [{"value": value, "value_classification": "Fear",
                     "timestamp": str(int(datetime(2018, 1, day,
                                                   tzinfo=timezone.utc).timestamp()))}
                    for day, value in sorted(self.fng.items(), reverse=True)]
            return FakeResponse({"name": "Fear and Greed Index", "data": data,
                                 "metadata": {"error": None}})
        if "fredgraph" in url:
            lines = ["observation_date,DFF"] + [
                f"2018-01-{day:02d},{value}" for day, value in sorted(self.fed.items())]
            return FakeResponse(text_body="\n".join(lines))
        raise AssertionError(f"Unexpected URL: {url}")


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_factors_load_and_repeat_changes_nothing(db):
    session = FactorSession()
    first = [run_loader(db, source, session, END) for source in ("fng", "fred")]
    second = [run_loader(db, source, session, END) for source in ("fng", "fred")]

    assert [r["rows_added"] for r in first] == [3, 3]
    assert [(r["rows_added"], r["rows_updated"]) for r in second] == [(0, 0), (0, 0)]
    assert count(db, raw_fng) == count(db, raw_fred) == 3
    # Повтор не порождает новых версий: значения не изменились
    assert count(db, external_factor_version) == 6


def test_revised_value_keeps_previous_version(db):
    session = FactorSession()
    run_loader(db, "fred", session, END)
    session.fed[12] = "1.50"   # источник пересмотрел значение задним числом
    result = run_loader(db, "fred", session, END)
    assert result["rows_updated"] == 1

    with db.connect() as conn:
        assert conn.execute(select(raw_fred.c.value).where(
            raw_fred.c.rate_date == date(2018, 1, 12))).scalar_one() == Decimal("1.5")
        versions = conn.execute(select(external_factor_version).where(
            external_factor_version.c.factor_date == date(2018, 1, 12)
        ).order_by(external_factor_version.c.version_no)).all()
        assert [v.version_no for v in versions] == [1, 2]
        assert versions[0].is_current is False and versions[0].valid_to is not None
        assert versions[1].is_current is True and versions[1].valid_to is None
        # До пересмотра система видела прежнее значение, после — новое
        assert value_as_of(conn, "fed_funds_rate", date(2018, 1, 12),
                           versions[0].valid_from) == Decimal("1.43")
        after = versions[1].valid_from + timedelta(seconds=1)
        assert value_as_of(conn, "fed_funds_rate", date(2018, 1, 12),
                           after) == Decimal("1.5")


def test_unpublished_fred_value_is_kept_in_raw_and_rejected_in_clean(db):
    session = FactorSession()
    session.fed[12] = "."      # так FRED передаёт отсутствующее значение
    run_loader(db, "fred", session, END)
    assert count(db, raw_fred) == 3
    build_clean(db, now=NOW)
    assert count(db, clean_fred) == 2
    with db.connect() as conn:
        failed = conn.execute(select(data_quality_log.c.check_name).where(
            data_quality_log.c.passed.is_(False))).scalars().all()
    assert "fred_not_null" in failed


def test_fed_rate_becomes_available_on_next_business_day():
    assert fed_available_from(date(2026, 9, 3)) == date(2026, 9, 4)   # чт -> пт
    assert fed_available_from(date(2026, 9, 4)) == date(2026, 9, 7)   # пт -> пн
    assert fed_available_from(date(2026, 9, 5)) == date(2026, 9, 7)   # сб -> пн


def _candle(conn, day):
    conn.execute(raw_okx.insert().values(
        instrument_id="BTC-USDT", candle_date=day, open=Decimal("100"),
        high=Decimal("110"), low=Decimal("90"), close=Decimal("105"),
        volume_btc=Decimal("10"), turnover_usdt=Decimal("1000"), confirm=1,
        source_json="[]", loaded_at=NOW))


def _row(db, day):
    with db.connect() as conn:
        return conn.execute(select(daily_mart).where(
            daily_mart.c.candle_date == day)).one()


def test_fed_rate_is_not_used_before_publication(db):
    with db.begin() as conn:
        for day in range(4, 8):   # пт 4 — пн 7 сентября 2026
            _candle(conn, date(2026, 9, day))
        for day, value in ((3, "1"), (4, "2")):
            conn.execute(raw_fred.insert().values(
                rate_date=date(2026, 9, day), series_id="DFF", value=Decimal(value),
                source_line="", loaded_at=NOW))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    # В пятницу, субботу и воскресенье известна только ставка за четверг
    for day, age in ((4, 1), (5, 2), (6, 3)):
        row = _row(db, date(2026, 9, day))
        assert (row.fed_rate, row.fed_rate_age_days) == (Decimal("1"), age)
    # Ставку за пятницу опубликуют в понедельник
    monday = _row(db, date(2026, 9, 7))
    assert (monday.fed_rate, monday.fed_rate_age_days) == (Decimal("2"), 3)


def test_fear_greed_gap_is_carried_forward_with_age(db):
    with db.begin() as conn:
        for day in (1, 2, 3):
            _candle(conn, date(2026, 9, day))
        for day, value in ((1, 20), (3, 60)):   # 2 сентября индекса нет
            conn.execute(raw_fng.insert().values(
                index_date=date(2026, 9, day), value=value, classification="x",
                source_json="{}", loaded_at=NOW))
    stats = build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    assert stats["fng_missing_days"] == 1
    gap = _row(db, date(2026, 9, 2))
    assert (gap.fear_greed, gap.fear_greed_age_days) == (20, 1)
    assert _row(db, date(2026, 9, 3)).fear_greed == 60


def test_migration_adds_columns_to_table_created_by_old_version():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def attach(connection, record):
        for schema in SCHEMAS:
            connection.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    # Таблица журнала в том виде, в каком её создавала первая версия проекта
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE load_log (id INTEGER PRIMARY KEY, source VARCHAR(50), "
            "started_at TIMESTAMP, status VARCHAR(20), rows_received INTEGER, "
            "rows_added INTEGER, last_loaded_date DATE, error_message TEXT)"))
    initialize_database(engine)
    initialize_database(engine)   # повторный запуск ничего не ломает

    columns = {c["name"] for c in inspect(engine).get_columns("load_log")}
    assert {"rows_updated", "run_id"} <= columns
    assert count(engine, load_log) == 0
    engine.dispose()
