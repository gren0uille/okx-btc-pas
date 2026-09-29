# Загрузчики слоя raw: суточные свечи OKX, курс USD/RUB Банка России,
# индекс страха и жадности alternative.me и ставка ФРС из FRED
# Повторный запуск догружает только новые даты

import argparse
import csv
import io
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from sqlalchemy import (
    Column, Date, DateTime, Integer, Numeric, String, Table, Text,
    create_engine, func, select, update,
)

from .db import initialize_database, metadata  # noqa: F401
from .history import record_version
from .source_probe import CBR_DYNAMIC_URL, OKX_HISTORY_URL, build_session

FNG_URL = "https://api.alternative.me/fng/"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = "DFF"   # эффективная ставка по федеральным фондам, % годовых

INSTRUMENT_ID = "BTC-USDT"
FIRST_OKX_DATE = date(2018, 1, 11)
FIRST_CBR_DATE = date(2018, 1, 1)
FIRST_FACTOR_DATE = date(2018, 1, 1)
UTC_DAY_MS = 86_400_000
# Индекс и ставка могут пересматриваться, поэтому последние дни
# запрашиваются повторно и сверяются с сохранёнными
REVISION_WINDOW_DAYS = 14
SOURCES = ("okx", "cbr", "fng", "fred")

raw_okx = Table(
    "okx_btc_usdt_daily", metadata,
    Column("instrument_id", String(20), primary_key=True),
    Column("candle_date", Date, primary_key=True),
    Column("open", Numeric(24, 8), nullable=False),
    Column("high", Numeric(24, 8), nullable=False),
    Column("low", Numeric(24, 8), nullable=False),
    Column("close", Numeric(24, 8), nullable=False),
    Column("volume_btc", Numeric(28, 8), nullable=False),
    Column("turnover_usdt", Numeric(30, 8), nullable=False),
    Column("confirm", Integer, nullable=False),
    Column("source_json", Text, nullable=False),
    Column("loaded_at", DateTime(timezone=True), nullable=False),
    schema="raw",
)
raw_cbr = Table(
    "cbr_usd_rub", metadata,
    Column("rate_date", Date, primary_key=True),
    Column("nominal", Integer, nullable=False),
    Column("value", Numeric(20, 6), nullable=False),
    Column("vunit_rate", Numeric(20, 6)),
    Column("source_xml", Text, nullable=False),
    Column("loaded_at", DateTime(timezone=True), nullable=False),
    schema="raw",
)
raw_fng = Table(
    "fear_greed_index", metadata,
    Column("index_date", Date, primary_key=True),
    Column("value", Integer),
    Column("classification", String(30)),
    Column("source_json", Text, nullable=False),
    Column("loaded_at", DateTime(timezone=True), nullable=False),
    schema="raw",
)
raw_fred = Table(
    "fred_fed_funds_rate", metadata,
    Column("rate_date", Date, primary_key=True),
    Column("series_id", String(20), nullable=False),
    # Пустое значение источник передаёт точкой: такая строка сохраняется,
    # а отбраковывается уже в слое clean
    Column("value", Numeric(10, 4)),
    Column("source_line", Text, nullable=False),
    Column("loaded_at", DateTime(timezone=True), nullable=False),
    schema="raw",
)
load_log = Table(
    "load_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(50), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False),
    Column("rows_received", Integer, nullable=False),
    Column("rows_added", Integer, nullable=False),
    Column("rows_updated", Integer, nullable=False, server_default="0"),
    Column("last_loaded_date", Date),
    Column("error_message", Text),
    Column("run_id", String(40)),
)


# Вставляем строку, пропуская конфликт по уникальному ключу
def _insert_new(conn, table, values):
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Only PostgreSQL and SQLite test backend are supported")
    result = conn.execute(insert(table).values(**values).on_conflict_do_nothing())
    return bool(result.rowcount)


def _utc_ms(day):
    return int(datetime.combine(day, time.min, timezone.utc).timestamp() * 1000)


def _candle_date(row):
    if len(row) != 9:
        raise ValueError("OKX candle must have nine fields")
    stamp = int(row[0])
    if stamp % UTC_DAY_MS:
        raise ValueError("OKX daily candle is not aligned to UTC midnight")
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).date()


# Читаем историю страницами назад, оставляя только нужные даты
def fetch_okx_candles(session, start, end):
    if start > end:
        return []
    cursor = _utc_ms(end + timedelta(days=1))
    start_ms = _utc_ms(start)
    result = []
    for _ in range(1000):
        response = session.get(
            OKX_HISTORY_URL,
            params={"instId": INSTRUMENT_ID, "bar": "1Dutc",
                    "after": str(cursor), "limit": "300"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0" or not isinstance(payload.get("data"), list):
            raise ValueError(f"Unexpected OKX response: {payload.get('msg', '')}")
        batch = payload["data"]
        if not batch:
            break
        stamps = [int(row[0]) for row in batch]
        oldest = min(stamps)
        if oldest >= cursor:
            raise ValueError("OKX pagination did not move backwards")
        result.extend(row for row in batch if start <= _candle_date(row) <= end)
        if oldest < start_ms:
            break
        cursor = oldest
    else:
        raise RuntimeError("OKX pagination exceeded the safety limit")
    return result


# Курс запрашиваем годовыми окнами: полная история одним запросом ненадёжна
def fetch_cbr_rates(session, start, end):
    result = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=364))
        response = session.get(
            CBR_DYNAMIC_URL,
            params={"date_req1": cursor.strftime("%d/%m/%Y"),
                    "date_req2": window_end.strftime("%d/%m/%Y"),
                    "VAL_NM_RQ": "R01235"},
            timeout=30,
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        if root.attrib.get("ID") != "R01235":
            raise ValueError("CBR returned a currency other than USD")
        for node in root.findall("Record"):
            fields = {"date": datetime.strptime(node.attrib["Date"], "%d.%m.%Y").date()}
            fields.update({child.tag: child.text for child in node})
            result.append((fields, ElementTree.tostring(node, encoding="unicode")))
        cursor = window_end + timedelta(days=1)
    return result


# Индекс страха и жадности: JSON, значение от 0 до 100 на сутки UTC
# Источник отдаёт последние limit дней; limit=0 означает всю историю
def fetch_fng(session, start, end):
    if start > end:
        return []
    days_back = (datetime.now(timezone.utc).date() - start).days + 2
    limit = 0 if start <= FIRST_FACTOR_DATE else days_back
    response = session.get(FNG_URL, params={"limit": limit, "format": "json"},
                           timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("data"), list):
        raise ValueError("Unexpected Fear & Greed response")
    if (payload.get("metadata") or {}).get("error"):
        raise ValueError(f"Fear & Greed error: {payload['metadata']['error']}")
    result = []
    for item in payload["data"]:
        stamp = int(item["timestamp"])
        if stamp % 86400:
            raise ValueError("Fear & Greed value is not aligned to UTC midnight")
        day = datetime.fromtimestamp(stamp, timezone.utc).date()
        if start <= day <= end:
            result.append((day, item))
    return result


# Ставка ФРС: CSV вида observation_date,DFF с одной строкой на дату
def fetch_fred(session, start, end, series=FRED_SERIES):
    if start > end:
        return []
    response = session.get(
        FRED_CSV_URL,
        params={"id": series, "cosd": start.isoformat(), "coed": end.isoformat()},
        timeout=60,
    )
    response.raise_for_status()
    reader = csv.reader(io.StringIO(response.text))
    header = next(reader, None)
    if not header or len(header) != 2 or header[1] != series:
        raise ValueError(f"Unexpected FRED header: {header}")
    result = []
    for line in reader:
        if len(line) != 2:
            raise ValueError(f"Unexpected FRED line: {line}")
        day = date.fromisoformat(line[0])
        if start <= day <= end:
            result.append((day, line))
    return result


def _decimal_or_none(text_value):
    if text_value is None or text_value.strip() in {"", "."}:
        return None
    try:
        return Decimal(text_value)
    except InvalidOperation:
        raise ValueError(f"Not a number: {text_value!r}")


def _load_okx(conn, session, start, end, started):
    received = added = 0
    rows = fetch_okx_candles(session, start, end)
    received = len(rows)
    for row in rows:
        candle_date = _candle_date(row)
        if row[8] not in {"0", "1"}:
            raise ValueError("Invalid OKX completion flag")
        if row[8] == "0":
            continue  # незавершённые сутки запросим повторно после закрытия
        values = {
            "instrument_id": INSTRUMENT_ID, "candle_date": candle_date,
            "open": Decimal(row[1]), "high": Decimal(row[2]),
            "low": Decimal(row[3]), "close": Decimal(row[4]),
            "volume_btc": Decimal(row[5]),
            "turnover_usdt": Decimal(row[7]), "confirm": 1,
            "source_json": json.dumps(row, ensure_ascii=False),
            "loaded_at": started,
        }
        added += int(_insert_new(conn, raw_okx, values))
    return received, added, 0


def _load_cbr(conn, session, start, end, started):
    rows = fetch_cbr_rates(session, start, end)
    added = 0
    for fields, xml in rows:
        rate_date = fields["date"]
        if not start <= rate_date <= end:
            raise ValueError("Unexpected CBR rate date")
        values = {
            "rate_date": rate_date, "nominal": int(fields["Nominal"]),
            "value": Decimal(fields["Value"].replace(",", ".")),
            "vunit_rate": (Decimal(fields["VunitRate"].replace(",", "."))
                           if fields.get("VunitRate") else None),
            "source_xml": xml, "loaded_at": started,
        }
        added += int(_insert_new(conn, raw_cbr, values))
    return len(rows), added, 0


# Общая загрузка пересматриваемого фактора: новые даты вставляются,
# изменившиеся обновляются в raw, каждое изменение попадает в hist
def _load_revisable(conn, table, date_column, records, factor_code, started, run_id):
    existing = {r[0]: r[1] for r in conn.execute(
        select(date_column, table.c.value).where(
            date_column.in_([values[date_column.name] for values in records]))
    )} if records else {}
    added = updated = 0
    for values in records:
        day = values[date_column.name]
        state = record_version(conn, factor_code, day, values["value"], started, run_id)
        if day not in existing:
            conn.execute(table.insert().values(**values))
            added += 1
        elif state == "changed":
            conn.execute(update(table).where(date_column == day).values(**values))
            updated += 1
    return added, updated


def _load_fng(conn, session, start, end, started, run_id):
    rows = fetch_fng(session, start, end)
    records = []
    for day, item in rows:
        value = item.get("value")
        records.append({
            "index_date": day,
            "value": int(value) if value not in (None, "") else None,
            "classification": item.get("value_classification"),
            "source_json": json.dumps(item, ensure_ascii=False),
            "loaded_at": started,
        })
    added, updated = _load_revisable(conn, raw_fng, raw_fng.c.index_date,
                                     records, "fear_greed", started, run_id)
    return len(rows), added, updated


def _load_fred(conn, session, start, end, started, run_id):
    rows = fetch_fred(session, start, end)
    records = [{
        "rate_date": day, "series_id": FRED_SERIES,
        "value": _decimal_or_none(line[1]),
        "source_line": ",".join(line), "loaded_at": started,
    } for day, line in rows]
    added, updated = _load_revisable(conn, raw_fred, raw_fred.c.rate_date,
                                     records, "fed_funds_rate", started, run_id)
    return len(rows), added, updated


_TABLES = {
    "okx": (raw_okx, raw_okx.c.candle_date, FIRST_OKX_DATE),
    "cbr": (raw_cbr, raw_cbr.c.rate_date, FIRST_CBR_DATE),
    "fng": (raw_fng, raw_fng.c.index_date, FIRST_FACTOR_DATE),
    "fred": (raw_fred, raw_fred.c.rate_date, FIRST_FACTOR_DATE),
}


# Сохраняем новые данные источника в одной транзакции и пишем итог в журнал
def run_loader(engine, source, session=None, end=None, run_id=None):
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    session = session or build_session()
    end = end or (datetime.now(timezone.utc).date() - timedelta(days=1))
    started = datetime.now(timezone.utc)
    received = added = updated = 0
    last_date = None
    status = "success"
    error = None
    _, date_column, first = _TABLES[source]
    try:
        with engine.begin() as conn:
            prior = conn.execute(select(func.max(date_column))).scalar_one()
            start = prior + timedelta(days=1) if prior else first
            if prior and source in ("fng", "fred"):
                start = max(first, prior - timedelta(days=REVISION_WINDOW_DAYS))
            if start <= end:
                if source == "okx":
                    received, added, updated = _load_okx(conn, session, start, end, started)
                elif source == "cbr":
                    received, added, updated = _load_cbr(conn, session, start, end, started)
                elif source == "fng":
                    received, added, updated = _load_fng(
                        conn, session, start, end, started, run_id)
                else:
                    received, added, updated = _load_fred(
                        conn, session, start, end, started, run_id)
            last_date = conn.execute(select(func.max(date_column))).scalar_one()
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"[:2000]
        added = updated = 0  # транзакция откатилась
    with engine.begin() as conn:
        conn.execute(load_log.insert().values(
            source=source, started_at=started, status=status,
            rows_received=received, rows_added=added, rows_updated=updated,
            last_loaded_date=last_date, error_message=error, run_id=run_id,
        ))
    result = {
        "source": source, "status": status, "rows_received": received,
        "rows_added": added, "rows_updated": updated,
        "last_loaded_date": str(last_date) if last_date else None,
        "error_message": error,
    }
    if status == "failed":
        raise RuntimeError(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Загрузка источников в слой raw")
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    initialize_database(engine)
    sources = list(SOURCES) if args.source == "all" else [args.source]
    failed = False
    for source in sources:
        try:
            print(json.dumps(run_loader(engine, source, end=args.end), ensure_ascii=False))
        except RuntimeError as exc:
            failed = True
            print(exc)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
