import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update

import okx_btc_pas.pipeline as pipeline
from okx_btc_pas.cleaning import build_clean
from okx_btc_pas.ingestion import raw_cbr, raw_fng, raw_fred, raw_okx
from okx_btc_pas.lineage import (
    EDGES, lineage_edge, pipeline_run, register_metadata, upstream,
)
from okx_btc_pas.mart import build_mart
from okx_btc_pas.model import forecast
from okx_btc_pas.quality import run_mart_checks, run_source_checks

LAST_DAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 19, 1, tzinfo=timezone.utc)


def _fill(db, last=LAST_DAY, days=40):
    with db.begin() as conn:
        for i in range(days):
            day = last - timedelta(days=i)
            row = ["0", "100", "110", "90", "105", "12.5", "1300", "1300", "1"]
            conn.execute(raw_okx.insert().values(
                instrument_id="BTC-USDT", candle_date=day, open=Decimal("100"),
                high=Decimal("110"), low=Decimal("90"), close=Decimal("105"),
                volume_btc=Decimal("12.5"), turnover_usdt=Decimal("1300"), confirm=1,
                source_json=json.dumps(row), loaded_at=NOW))
            conn.execute(raw_cbr.insert().values(
                rate_date=day, nominal=1, value=Decimal("90"), vunit_rate=None,
                source_xml="<Record><Nominal>1</Nominal><Value>90,0</Value></Record>",
                loaded_at=NOW))
            conn.execute(raw_fng.insert().values(
                index_date=day, value=50, classification="Neutral",
                source_json=json.dumps({"value": "50"}), loaded_at=NOW))
            conn.execute(raw_fred.insert().values(
                rate_date=day, series_id="DFF", value=Decimal("4.33"),
                source_line="", loaded_at=NOW))


def test_checks_pass_on_consistent_data(db):
    _fill(db)
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)
    assert run_source_checks(db, run_id="r", now=NOW)["critical"] == []
    assert run_mart_checks(db, run_id="r", now=NOW)["critical"] == []


def test_stale_exchange_data_is_critical(db):
    _fill(db, last=LAST_DAY - timedelta(days=5))
    build_clean(db, now=NOW)
    critical = run_source_checks(db, run_id="r", now=NOW)["critical"]
    assert any("freshness" in item and "okx" in item for item in critical)


def test_typed_value_differing_from_source_is_detected(db):
    _fill(db)
    with db.begin() as conn:   # ошибка разбора: столбец не совпадает с ответом
        conn.execute(update(raw_okx).where(raw_okx.c.candle_date == LAST_DAY)
                     .values(volume_btc=Decimal("999")))
    build_clean(db, now=NOW)
    critical = run_source_checks(db, run_id="r", now=NOW)["critical"]
    assert any("typed_matches_source" in item for item in critical)


def test_many_rejected_rows_are_critical(db):
    _fill(db)
    with db.begin() as conn:   # максимум ниже минимума — строки отбракуются
        conn.execute(update(raw_okx).where(raw_okx.c.candle_date >= LAST_DAY - timedelta(days=3))
                     .values(high=Decimal("80")))
    build_clean(db, now=NOW)
    critical = run_source_checks(db, run_id="r", now=NOW)["critical"]
    assert any("reject_share" in item for item in critical)


def test_pipeline_stops_before_forecast_on_critical_problem(db, monkeypatch):
    _fill(db, last=LAST_DAY - timedelta(days=5))   # биржевые данные устарели

    def failing_loader(engine, source, session=None, run_id=None):
        raise RuntimeError({"error_message": "source unavailable"})

    monkeypatch.setattr(pipeline, "run_loader", failing_loader)
    result = pipeline.run_pipeline(db, now=NOW)

    assert result["status"] == "stopped_by_quality"
    assert "mart" not in result["steps"]
    with db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(forecast)).scalar_one() == 0
        run = conn.execute(select(pipeline_run)).one()
    assert run.status == "stopped_by_quality"
    assert "source unavailable" in run.steps


def test_dashboard_metric_is_traced_to_source_field(db):
    register_metadata(db)
    register_metadata(db)   # повторная регистрация не дублирует рёбра
    with db.connect() as conn:
        edges = conn.execute(select(func.count()).select_from(lineage_edge)).scalar_one()
        chain = upstream(conn, "dashboard.market", "Волатильность: факт")
    assert edges == len(EDGES)
    datasets = {edge.from_dataset for edge in chain}
    assert {"mart.daily_market_mart", "clean.okx_btc_usdt_daily",
            "raw.okx_btc_usdt_daily", "source.okx_candles"} <= datasets
