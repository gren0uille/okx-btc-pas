# Слой clean: типизированные и проверенные суточные строки из слоя raw
# Отбракованная строка не исчезает молча, а попадает в data_quality_log:
# строк в raw = строк в clean + зафиксированных отбраковок

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Integer, Numeric, String, Table, Text,
    create_engine, delete, func, select,
)
from .ingestion import (
    initialize_database, metadata, raw_cbr, raw_fng, raw_fred, raw_okx,
)


# Свеча за пределами диапазона считается невозможной для BTC/USDT
# и отбраковывается, а не попадает в модель незаметно
MIN_PRICE = Decimal("1")
MAX_PRICE = Decimal("100000000")
# Эффективная ставка ФРС за всю историю не выходила за эти границы
MAX_FED_RATE = Decimal("25")

clean_okx = Table(
    "okx_btc_usdt_daily", metadata,
    Column("instrument_id", String(20), primary_key=True),
    Column("candle_date", Date, primary_key=True),
    Column("open", Numeric(24, 8), nullable=False),
    Column("high", Numeric(24, 8), nullable=False),
    Column("low", Numeric(24, 8), nullable=False),
    Column("close", Numeric(24, 8), nullable=False),
    Column("volume_btc", Numeric(28, 8), nullable=False),
    Column("turnover_usdt", Numeric(30, 8), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    schema="clean",
)
clean_cbr = Table(
    "cbr_usd_rub", metadata,
    Column("rate_date", Date, primary_key=True),
    # Курс ЦБ указан за Nominal единиц, поэтому храним курс за одну единицу
    Column("rate_per_usd", Numeric(20, 6), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    schema="clean",
)
clean_fng = Table(
    "fear_greed_index", metadata,
    Column("index_date", Date, primary_key=True),
    Column("value", Integer, nullable=False),
    Column("classification", String(30)),
    Column("built_at", DateTime(timezone=True), nullable=False),
    schema="clean",
)
clean_fred = Table(
    "fed_funds_rate", metadata,
    Column("rate_date", Date, primary_key=True),
    Column("rate_pct", Numeric(10, 4), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    schema="clean",
)
data_quality_log = Table(
    "data_quality_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("layer", String(20), nullable=False),
    Column("table_name", String(60), nullable=False),
    Column("check_name", String(60), nullable=False),
    Column("check_type", String(30), nullable=False),
    Column("entity_key", String(60)),
    Column("passed", Boolean, nullable=False),
    Column("details", Text),
    Column("run_id", String(40)),
    # error — строка отбракована; warning — отмечено, работа продолжается;
    # critical — конвейер останавливается до построения прогноза
    Column("severity", String(20), nullable=False, server_default="error"),
    # Что система сделала: reject_row, log_only, carry_forward, stop_pipeline
    Column("action", String(30), nullable=False, server_default="reject_row"),
)


# Пишем результат одной проверки в журнал качества
def log_check(conn, moment, table_name, check, check_type, key, passed, details,
              severity="error", action="reject_row", layer="clean", run_id=None):
    conn.execute(data_quality_log.insert().values(
        checked_at=moment, layer=layer, table_name=table_name,
        check_name=check, check_type=check_type, entity_key=key,
        passed=passed, details=details, severity=severity, action=action,
        run_id=run_id,
    ))


# Возвращаем список непройденных проверок для одной свечи
# Пустой список означает, что строка годится для слоя clean
def validate_candle(row):
    problems = []
    prices = {"open": row.open, "high": row.high, "low": row.low, "close": row.close}

    missing = [name for name, value in prices.items() if value is None]
    if row.volume_btc is None:
        missing.append("volume_btc")
    if missing:
        problems.append(("candle_not_null", "completeness",
                         f"пустые поля: {', '.join(missing)}"))
        return problems

    out_of_range = [f"{name}={value}" for name, value in prices.items()
                    if not MIN_PRICE <= value <= MAX_PRICE]
    if out_of_range:
        problems.append(("candle_price_range", "range",
                         f"цена вне допустимого диапазона: {', '.join(out_of_range)}"))

    if row.high < row.low:
        problems.append(("candle_high_low", "validity",
                         f"high={row.high} меньше low={row.low}"))
    else:
        for name in ("open", "close"):
            value = prices[name]
            if not row.low <= value <= row.high:
                problems.append(("candle_open_close_bounds", "validity",
                                 f"{name}={value} вне интервала [{row.low}; {row.high}]"))

    if row.volume_btc < 0:
        problems.append(("candle_volume_sign", "range",
                         f"отрицательный объём: {row.volume_btc}"))
    if row.turnover_usdt is not None and row.turnover_usdt < 0:
        problems.append(("candle_turnover_sign", "range",
                         f"отрицательный оборот: {row.turnover_usdt}"))
    return problems


# Возвращаем список непройденных проверок для одной записи курса
def validate_rate(row):
    problems = []
    if row.value is None or row.nominal is None:
        problems.append(("rate_not_null", "completeness", "пустой курс или номинал"))
        return problems
    if row.nominal <= 0:
        problems.append(("rate_nominal_positive", "validity",
                         f"номинал не положителен: {row.nominal}"))
    if row.value <= 0:
        problems.append(("rate_value_positive", "range",
                         f"курс не положителен: {row.value}"))
    return problems


# Индекс страха и жадности по определению лежит в диапазоне 0–100
def validate_fng(row):
    if row.value is None:
        return [("fng_not_null", "completeness", "пустое значение индекса")]
    if not 0 <= row.value <= 100:
        return [("fng_range", "range", f"индекс вне диапазона 0–100: {row.value}")]
    return []


# Ставка ФРС: пустое значение источник передаёт точкой
def validate_fred(row):
    if row.value is None:
        return [("fred_not_null", "completeness", "значение ставки не опубликовано")]
    if not Decimal("0") <= row.value <= MAX_FED_RATE:
        return [("fred_range", "range", f"ставка вне диапазона 0–25 %: {row.value}")]
    return []


# Перенос строк одной таблицы raw в clean с проверками
# Непройденная проверка пишется построчно, с датой, чтобы найти причину.
# Итог по таблице — одной сводной строкой: иначе журнал рос бы на тысячи
# записей при каждом запуске и терял бы читаемость
def _rebuild(conn, moment, run_id, source, target, key_column, validate, to_values,
             table_name, ok_check):
    kept_rows = []
    rejected = 0
    for row in conn.execute(select(source).order_by(key_column)).all():
        key = str(getattr(row, key_column.name))
        problems = validate(row)
        for check, check_type, details in problems:
            log_check(conn, moment, table_name, check, check_type, key, False,
                      details, run_id=run_id)
        if problems:
            rejected += 1
            continue
        kept_rows.append({**to_values(row), "built_at": moment})
    if kept_rows:
        conn.execute(target.insert(), kept_rows)
    total = len(kept_rows) + rejected
    log_check(conn, moment, table_name, ok_check, "validity", None, rejected == 0,
              f"проверено {total}, принято {len(kept_rows)}, отбраковано {rejected}",
              severity="info" if rejected == 0 else "warning",
              action="log_only" if rejected == 0 else "reject_row", run_id=run_id)
    return len(kept_rows), rejected


# Перестраиваем слой clean из raw и пишем результаты всех проверок
# Слой пересобирается целиком в одной транзакции, поэтому повторный
# запуск даёт то же состояние таблиц
def build_clean(engine, now=None, run_id=None):
    moment = now or datetime.now(timezone.utc)
    stats = {}

    initialize_database(engine)

    with engine.begin() as conn:
        for table in (clean_okx, clean_cbr, clean_fng, clean_fred):
            conn.execute(delete(table))

        stats["okx_rows"], stats["okx_rejected"] = _rebuild(
            conn, moment, run_id, raw_okx, clean_okx, raw_okx.c.candle_date,
            validate_candle,
            lambda r: {"instrument_id": r.instrument_id, "candle_date": r.candle_date,
                       "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                       "volume_btc": r.volume_btc, "turnover_usdt": r.turnover_usdt},
            "clean.okx_btc_usdt_daily", "candle_all_checks")

        stats["cbr_rows"], stats["cbr_rejected"] = _rebuild(
            conn, moment, run_id, raw_cbr, clean_cbr, raw_cbr.c.rate_date,
            validate_rate,
            lambda r: {"rate_date": r.rate_date, "rate_per_usd": r.value / r.nominal},
            "clean.cbr_usd_rub", "rate_all_checks")

        stats["fng_rows"], stats["fng_rejected"] = _rebuild(
            conn, moment, run_id, raw_fng, clean_fng, raw_fng.c.index_date,
            validate_fng,
            lambda r: {"index_date": r.index_date, "value": r.value,
                       "classification": r.classification},
            "clean.fear_greed_index", "fng_all_checks")

        stats["fred_rows"], stats["fred_rejected"] = _rebuild(
            conn, moment, run_id, raw_fred, clean_fred, raw_fred.c.rate_date,
            validate_fred,
            lambda r: {"rate_date": r.rate_date, "rate_pct": r.value},
            "clean.fed_funds_rate", "fred_all_checks")

        gaps = check_calendar_gaps(conn, moment, clean_okx.c.candle_date,
                                   "clean.okx_btc_usdt_daily",
                                   "candle_calendar_continuity", "log_only", run_id)
        stats["expected_days"], stats["missing_days"] = gaps
        gaps = check_calendar_gaps(conn, moment, clean_fng.c.index_date,
                                   "clean.fear_greed_index",
                                   "fng_calendar_continuity", "carry_forward", run_id)
        stats["fng_missing_days"] = gaps[1]

    return stats


# Ищем пропущенные календарные даты в ежедневном ряде
# Пропуск отмечается предупреждением: строки не удаляются, а в витрине
# для такого дня берётся последнее известное значение с указанием возраста
def check_calendar_gaps(conn, moment, date_column, table_name, check, action, run_id):
    first, last = conn.execute(
        select(func.min(date_column), func.max(date_column))).one()
    if first is None:
        return 0, 0
    present = {r[0] for r in conn.execute(select(date_column))}
    expected = (last - first).days + 1
    missing = sorted({first + timedelta(days=n) for n in range(expected)} - present)
    log_check(conn, moment, table_name, check, "completeness", None, not missing,
              None if not missing else
              f"пропущено дат: {len(missing)}; первые: "
              f"{', '.join(str(d) for d in missing[:5])}",
              severity="info" if not missing else "warning", action=action,
              run_id=run_id)
    return expected, len(missing)


def main():
    parser = argparse.ArgumentParser(description="Построение слоя clean")
    parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    initialize_database(engine)
    print(json.dumps(build_clean(engine), ensure_ascii=False))


if __name__ == "__main__":
    main()
