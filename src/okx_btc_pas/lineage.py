# Управление метаданными и происхождение данных
#
# meta.dataset         — каталог наборов: слой, назначение, гранулярность, ключ
# meta.dataset_column  — описание ключевых столбцов и единиц измерения
# meta.lineage_edge    — рёбра происхождения: из какого столбца какого набора
#                        и каким преобразованием получен столбец следующего
# meta.pipeline_run    — журнал запусков конвейера целиком
#
# Команда trace проходит рёбра в обратную сторону от показателя на
# дашборде до поля ответа источника и показывает значения на выбранную дату:
#   python -m okx_btc_pas.lineage --column volatility --date 2026-09-18

import argparse
import json
import math
import os
from datetime import date, datetime, timezone

from sqlalchemy import (
    Column, DateTime, Integer, String, Table, Text, create_engine, delete, select,
    text,
)

from .db import initialize_database, metadata

dataset = Table(
    "dataset", metadata,
    Column("dataset_name", String(80), primary_key=True),
    Column("layer", String(20), nullable=False),
    Column("description", Text, nullable=False),
    Column("grain", String(120), nullable=False),
    Column("primary_key", String(120)),
    Column("produced_by", String(120), nullable=False),
    Column("source_url", String(200)),
    schema="meta",
)
dataset_column = Table(
    "dataset_column", metadata,
    Column("dataset_name", String(80), primary_key=True),
    Column("column_name", String(60), primary_key=True),
    Column("description", Text, nullable=False),
    Column("unit", String(40)),
    schema="meta",
)
lineage_edge = Table(
    "lineage_edge", metadata,
    Column("edge_id", Integer, primary_key=True),
    Column("from_dataset", String(80), nullable=False),
    Column("from_column", String(60), nullable=False),
    Column("to_dataset", String(80), nullable=False),
    Column("to_column", String(60), nullable=False),
    Column("transformation", Text, nullable=False),
    Column("process", String(120), nullable=False),
    schema="meta",
)
pipeline_run = Table(
    "pipeline_run", metadata,
    Column("run_id", String(40), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("status", String(20), nullable=False),
    Column("steps", Text),
    Column("error_message", Text),
    schema="meta",
)

OKX = "https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1Dutc"
CBR = "https://www.cbr.ru/scripts/XML_dynamic.asp?VAL_NM_RQ=R01235"
FNG = "https://api.alternative.me/fng/"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"

DATASETS = [
    ("source.okx_candles", "source", "Суточные свечи BTC/USDT, публичный JSON API OKX",
     "сутки UTC", "ts", "внешний источник", OKX),
    ("source.cbr_rates", "source", "Официальный курс USD/RUB, XML Банка России",
     "дата установления курса", "Date", "внешний источник", CBR),
    ("source.fear_greed", "source", "Индекс страха и жадности, JSON alternative.me",
     "сутки UTC", "timestamp", "внешний источник", FNG),
    ("source.fred_dff", "source", "Эффективная ставка ФРС, CSV FRED",
     "календарная дата", "observation_date", "внешний источник", FRED),
    ("raw.okx_btc_usdt_daily", "raw", "Завершённые свечи с исходным JSON",
     "инструмент × сутки UTC", "instrument_id, candle_date",
     "ingestion.run_loader('okx')", OKX),
    ("raw.cbr_usd_rub", "raw", "Курс ЦБ с исходным XML", "дата курса", "rate_date",
     "ingestion.run_loader('cbr')", CBR),
    ("raw.fear_greed_index", "raw", "Индекс страха и жадности с исходным JSON",
     "сутки UTC", "index_date", "ingestion.run_loader('fng')", FNG),
    ("raw.fred_fed_funds_rate", "raw", "Ставка ФРС с исходной строкой CSV",
     "календарная дата", "rate_date", "ingestion.run_loader('fred')", FRED),
    ("hist.external_factor_version", "hist",
     "Версии значений пересматриваемых факторов (SCD Type 2)",
     "фактор × дата × версия", "factor_code, factor_date, version_no",
     "history.record_version", None),
    ("clean.okx_btc_usdt_daily", "clean", "Свечи, прошедшие проверки качества",
     "инструмент × сутки UTC", "instrument_id, candle_date",
     "cleaning.build_clean", None),
    ("clean.cbr_usd_rub", "clean", "Курс за одну единицу валюты", "дата курса",
     "rate_date", "cleaning.build_clean", None),
    ("clean.fear_greed_index", "clean", "Проверенный индекс 0–100", "сутки UTC",
     "index_date", "cleaning.build_clean", None),
    ("clean.fed_funds_rate", "clean", "Проверенная ставка, % годовых",
     "календарная дата", "rate_date", "cleaning.build_clean", None),
    ("mart.daily_market_mart", "mart",
     "Витрина: признаки суток d и целевые значения суток d+1", "сутки UTC",
     "candle_date", "mart.build_mart", None),
    ("mart.forecast", "mart", "Прогнозы всех запусков: проверочные и на завтра",
     "запуск × показатель × модель × дата", "run_id, target, model_name, "
     "feature_set, target_date", "model.run_models", None),
    ("mart.model_run", "mart", "Метрики моделей по запускам", "запуск × модель",
     "run_id, target, model_name, feature_set", "model.run_models", None),
    ("dashboard.market", "dashboard", "Предметный дашборд Superset",
     "график", None, "superset/bootstrap.py", None),
    ("dashboard.operations", "dashboard", "Операционный дашборд Superset",
     "график", None, "superset/bootstrap.py", None),
]

COLUMNS = [
    ("mart.daily_market_mart", "volume_btc", "Объём торгов за сутки", "BTC"),
    ("mart.daily_market_mart", "volatility_pk",
     "Волатильность Паркинсона |ln(high/low)|/(2√ln2)", "безразмерная"),
    ("mart.daily_market_mart", "target_volume_btc", "Объём следующих суток (цель)", "BTC"),
    ("mart.daily_market_mart", "target_volatility_pk",
     "Волатильность следующих суток (цель)", "безразмерная"),
    ("mart.daily_market_mart", "usd_rub", "Курс ЦБ, опубликованный к концу суток", "RUB"),
    ("mart.daily_market_mart", "fear_greed", "Индекс страха и жадности", "0–100"),
    ("mart.daily_market_mart", "fed_rate",
     "Ставка ФРС, опубликованная к концу суток", "% годовых"),
    ("mart.forecast", "y_pred", "Прогноз показателя на target_date", "как у цели"),
    ("mart.forecast", "lower_80", "Нижняя граница 80-процентного интервала", "как у цели"),
    ("mart.forecast", "upper_80", "Верхняя граница 80-процентного интервала", "как у цели"),
]

# (из набора, из столбца, в набор, в столбец, преобразование, процесс)
EDGES = [
    ("source.okx_candles", "data[i][2] h", "raw.okx_btc_usdt_daily", "high",
     "строка → Decimal, только confirm = 1", "ingestion.run_loader"),
    ("source.okx_candles", "data[i][3] l", "raw.okx_btc_usdt_daily", "low",
     "строка → Decimal, только confirm = 1", "ingestion.run_loader"),
    ("source.okx_candles", "data[i][5] vol", "raw.okx_btc_usdt_daily", "volume_btc",
     "строка → Decimal, только confirm = 1", "ingestion.run_loader"),
    ("raw.okx_btc_usdt_daily", "high", "clean.okx_btc_usdt_daily", "high",
     "проверки диапазона и high ≥ low", "cleaning.build_clean"),
    ("raw.okx_btc_usdt_daily", "low", "clean.okx_btc_usdt_daily", "low",
     "проверки диапазона и high ≥ low", "cleaning.build_clean"),
    ("raw.okx_btc_usdt_daily", "volume_btc", "clean.okx_btc_usdt_daily", "volume_btc",
     "проверка неотрицательности", "cleaning.build_clean"),
    ("clean.okx_btc_usdt_daily", "high", "mart.daily_market_mart", "volatility_pk",
     "|ln(high/low)| / (2√ln2)", "mart.parkinson"),
    ("clean.okx_btc_usdt_daily", "low", "mart.daily_market_mart", "volatility_pk",
     "|ln(high/low)| / (2√ln2)", "mart.parkinson"),
    ("clean.okx_btc_usdt_daily", "volume_btc", "mart.daily_market_mart", "volume_btc",
     "без изменений", "mart.build_mart"),
    ("mart.daily_market_mart", "volatility_pk", "mart.daily_market_mart",
     "target_volatility_pk", "значение суток d+1", "mart.build_mart"),
    ("mart.daily_market_mart", "volume_btc", "mart.daily_market_mart",
     "target_volume_btc", "значение суток d+1", "mart.build_mart"),
    ("source.cbr_rates", "Value / Nominal", "raw.cbr_usd_rub", "value",
     "запятая → точка, строка → Decimal", "ingestion.run_loader"),
    ("raw.cbr_usd_rub", "value", "clean.cbr_usd_rub", "rate_per_usd",
     "value / nominal", "cleaning.build_clean"),
    ("clean.cbr_usd_rub", "rate_per_usd", "mart.daily_market_mart", "usd_rub",
     "последний курс с датой ≤ d", "mart.AsOf"),
    ("source.fear_greed", "data[i].value", "raw.fear_greed_index", "value",
     "строка → целое", "ingestion.run_loader"),
    ("raw.fear_greed_index", "value", "hist.external_factor_version", "value",
     "новая версия при изменении значения", "history.record_version"),
    ("raw.fear_greed_index", "value", "clean.fear_greed_index", "value",
     "проверка диапазона 0–100", "cleaning.build_clean"),
    ("clean.fear_greed_index", "value", "mart.daily_market_mart", "fear_greed",
     "последнее значение с датой ≤ d", "mart.AsOf"),
    ("source.fred_dff", "DFF", "raw.fred_fed_funds_rate", "value",
     "строка → Decimal, «.» → пусто", "ingestion.run_loader"),
    ("raw.fred_fed_funds_rate", "value", "hist.external_factor_version", "value",
     "новая версия при изменении значения", "history.record_version"),
    ("raw.fred_fed_funds_rate", "value", "clean.fed_funds_rate", "rate_pct",
     "проверка диапазона 0–25 %", "cleaning.build_clean"),
    ("clean.fed_funds_rate", "rate_pct", "mart.daily_market_mart", "fed_rate",
     "последняя ставка, опубликованная к концу суток d", "mart.AsOf"),
    ("mart.daily_market_mart", "volatility_pk", "mart.forecast", "y_pred",
     "модель-чемпион, признаки суток d", "model.run_models"),
    ("mart.daily_market_mart", "volume_btc", "mart.forecast", "y_pred",
     "модель-чемпион, признаки суток d", "model.run_models"),
    ("mart.daily_market_mart", "volatility_pk", "dashboard.market",
     "Волатильность: факт", "mart.v_market_daily", "superset"),
    ("mart.daily_market_mart", "volume_btc", "dashboard.market",
     "Объём торгов: факт", "mart.v_market_daily", "superset"),
    ("mart.forecast", "y_pred", "dashboard.market", "Прогноз на завтра",
     "mart.v_next_day_forecast", "superset"),
]


# Записываем каталог и рёбра. Перезапись целиком: метаданные описаны в коде,
# поэтому повторный запуск приводит таблицы в то же состояние
def register_metadata(engine):
    initialize_database(engine)
    with engine.begin() as conn:
        for table in (lineage_edge, dataset_column, dataset):
            conn.execute(delete(table))
        conn.execute(dataset.insert(), [dict(zip(
            ("dataset_name", "layer", "description", "grain", "primary_key",
             "produced_by", "source_url"), row)) for row in DATASETS])
        conn.execute(dataset_column.insert(), [dict(zip(
            ("dataset_name", "column_name", "description", "unit"), row))
            for row in COLUMNS])
        conn.execute(lineage_edge.insert(), [dict(zip(
            ("edge_id", "from_dataset", "from_column", "to_dataset", "to_column",
             "transformation", "process"), (i, *row))) for i, row in enumerate(EDGES, 1)])
    return {"datasets": len(DATASETS), "columns": len(COLUMNS), "edges": len(EDGES)}


# Все рёбра, ведущие к столбцу, в обратном порядке — до источника
def upstream(conn, to_dataset, to_column, seen=None):
    seen = seen or set()
    chain = []
    edges = conn.execute(select(lineage_edge).where(
        lineage_edge.c.to_dataset == to_dataset,
        lineage_edge.c.to_column == to_column)).all()
    for edge in edges:
        key = (edge.from_dataset, edge.from_column)
        if key in seen:
            continue
        seen.add(key)
        chain.append(edge)
        chain.extend(upstream(conn, edge.from_dataset, edge.from_column, seen))
    return chain


# Значения показателя на каждом шаге цепочки для конкретной даты
def sample_values(conn, day):
    params = {"d": day}
    queries = {
        "mart.daily_market_mart": "SELECT volume_btc, volatility_pk FROM "
                                  "mart.daily_market_mart WHERE candle_date = :d",
        "clean.okx_btc_usdt_daily": "SELECT high, low, volume_btc FROM "
                                    "clean.okx_btc_usdt_daily WHERE candle_date = :d",
        "raw.okx_btc_usdt_daily": "SELECT high, low, volume_btc, source_json FROM "
                                  "raw.okx_btc_usdt_daily WHERE candle_date = :d",
    }
    return {name: conn.execute(text(sql), params).mappings().first()
            for name, sql in queries.items()}


DASHBOARD_METRICS = {
    "volatility": ("dashboard.market", "Волатильность: факт"),
    "volume": ("dashboard.market", "Объём торгов: факт"),
    "forecast": ("dashboard.market", "Прогноз на завтра"),
}


def trace(engine, metric, day):
    to_dataset, to_column = DASHBOARD_METRICS[metric]
    with engine.connect() as conn:
        chain = upstream(conn, to_dataset, to_column)
        values = sample_values(conn, day)
    lines = [f"Показатель «{to_column}» ({to_dataset}), дата {day}", ""]
    for edge in chain:
        lines.append(f"  {edge.to_dataset}.{edge.to_column}")
        lines.append(f"    ← {edge.from_dataset}.{edge.from_column}")
        lines.append(f"      преобразование: {edge.transformation}; процесс: {edge.process}")
    lines.append("")
    for name, row in values.items():
        if row is not None:
            shown = {k: (v if k != "source_json" else json.loads(v))
                     for k, v in dict(row).items()}
            lines.append(f"  {name}: {shown}")
    raw = values.get("raw.okx_btc_usdt_daily")
    mart_row = values.get("mart.daily_market_mart")
    if metric == "volatility" and raw and mart_row:
        source = json.loads(raw["source_json"])
        high, low = float(source[2]), float(source[3])
        recomputed = abs(math.log(high / low)) / (2 * math.sqrt(math.log(2)))
        lines.append("")
        lines.append(f"  Пересчёт из ответа источника: |ln({source[2]} / {source[3]})| "
                     f"/ (2√ln2) = {recomputed:.10f}")
        lines.append(f"  Значение в витрине: {float(mart_row['volatility_pk']):.10f}")
    return "\n".join(lines)


def record_run(engine, run_id, started_at, status, steps, error=None):
    with engine.begin() as conn:
        conn.execute(delete(pipeline_run).where(pipeline_run.c.run_id == run_id))
        conn.execute(pipeline_run.insert().values(
            run_id=run_id, started_at=started_at,
            finished_at=datetime.now(timezone.utc), status=status,
            steps=json.dumps(steps, ensure_ascii=False, default=str),
            error_message=error))


def main():
    parser = argparse.ArgumentParser(description="Происхождение показателя")
    parser.add_argument("--metric", choices=list(DASHBOARD_METRICS), default="volatility")
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    register_metadata(engine)
    print(trace(engine, args.metric, args.date))


if __name__ == "__main__":
    main()
