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
from .ingestion import initialize_database, metadata, raw_cbr, raw_okx


# Свеча за пределами диапазона считается невозможной для BTC/USDT
# и отбраковывается, а не попадает в модель незаметно
MIN_PRICE = Decimal("1")
MAX_PRICE = Decimal("100000000")

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
    # Value is per Nominal units, so the per-unit rate is stored explicitly.
    Column("rate_per_usd", Numeric(20, 6), nullable=False),
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
)


# Пишем результат одной проверки в журнал качества
def _log(conn, moment, table_name, check, check_type, key, passed, details):
    conn.execute(data_quality_log.insert().values(
        checked_at=moment, layer="clean", table_name=table_name,
        check_name=check, check_type=check_type, entity_key=key,
        passed=passed, details=details,
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


# Перестраиваем слой clean из raw и пишем результаты всех проверок
# Слой пересобирается целиком в одной транзакции, поэтому повторный
# запуск даёт то же состояние таблиц
def build_clean(engine, now=None):
    moment = now or datetime.now(timezone.utc)
    stats = {"okx_rows": 0, "okx_rejected": 0, "cbr_rows": 0, "cbr_rejected": 0}

    initialize_database(engine)

    with engine.begin() as conn:
        conn.execute(delete(clean_okx))
        conn.execute(delete(clean_cbr))

        for row in conn.execute(select(raw_okx).order_by(raw_okx.c.candle_date)):
            key = str(row.candle_date)
            problems = validate_candle(row)
            for check, check_type, details in problems:
                _log(conn, moment, "clean.okx_btc_usdt_daily", check,
                     check_type, key, False, details)
            if problems:
                stats["okx_rejected"] += 1
                continue
            _log(conn, moment, "clean.okx_btc_usdt_daily", "candle_all_checks",
                 "validity", key, True, None)
            conn.execute(clean_okx.insert().values(
                instrument_id=row.instrument_id, candle_date=row.candle_date,
                open=row.open, high=row.high, low=row.low, close=row.close,
                volume_btc=row.volume_btc, turnover_usdt=row.turnover_usdt,
                built_at=moment,
            ))
            stats["okx_rows"] += 1

        for row in conn.execute(select(raw_cbr).order_by(raw_cbr.c.rate_date)):
            key = str(row.rate_date)
            problems = validate_rate(row)
            for check, check_type, details in problems:
                _log(conn, moment, "clean.cbr_usd_rub", check,
                     check_type, key, False, details)
            if problems:
                stats["cbr_rejected"] += 1
                continue
            _log(conn, moment, "clean.cbr_usd_rub", "rate_all_checks",
                 "validity", key, True, None)
            conn.execute(clean_cbr.insert().values(
                rate_date=row.rate_date,
                rate_per_usd=row.value / row.nominal,
                built_at=moment,
            ))
            stats["cbr_rows"] += 1

        stats.update(check_calendar_gaps(conn, moment))

    return stats


# Ищем пропущенные календарные даты в ряде свечей
# Биржа работает ежедневно, поэтому пропуск означает потерю данных,
# а не выходной день. Строки при этом не удаляются
def check_calendar_gaps(conn, moment):
    bounds = conn.execute(
        select(func.min(clean_okx.c.candle_date), func.max(clean_okx.c.candle_date))
    ).one()
    first, last = bounds
    if first is None:
        return {"expected_days": 0, "missing_days": 0}

    present = {r[0] for r in conn.execute(select(clean_okx.c.candle_date))}
    expected = (last - first).days + 1
    missing = sorted({first + _days(n) for n in range(expected)} - present)
    _log(conn, moment, "clean.okx_btc_usdt_daily", "candle_calendar_continuity",
         "completeness", None, not missing,
         None if not missing else
         f"пропущено дат: {len(missing)}; первые: "
         f"{', '.join(str(d) for d in missing[:5])}")
    return {"expected_days": expected, "missing_days": len(missing)}


def _days(count):
    return timedelta(days=count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    initialize_database(engine)
    print(json.dumps(build_clean(engine), ensure_ascii=False))


if __name__ == "__main__":
    main()
