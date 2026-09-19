"""Incremental raw loaders for OKX BTC-USDT candles and CBR USD/RUB rates."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from xml.etree import ElementTree

import requests
from sqlalchemy import (
    Column, Date, DateTime, Integer, MetaData, Numeric, String, Table, Text,
    create_engine, func, select, text,
)
from sqlalchemy.engine import Connection, Engine

from .source_probe import CBR_DYNAMIC_URL, OKX_HISTORY_URL, build_session


INSTRUMENT_ID = "BTC-USDT"
FIRST_OKX_DATE = date(2018, 1, 11)
FIRST_CBR_DATE = date(2018, 1, 1)
UTC_DAY_MS = 86_400_000
metadata = MetaData()

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
load_log = Table(
    "load_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(50), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False),
    Column("rows_received", Integer, nullable=False),
    Column("rows_added", Integer, nullable=False),
    Column("last_loaded_date", Date),
    Column("error_message", Text),
)


def initialize_database(engine: Engine) -> None:
    """Create the stage-two raw and load-log tables."""
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS raw"))
    metadata.create_all(engine)


def _insert_new(conn: Connection, table: Table, values: dict) -> bool:
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Only PostgreSQL and SQLite test backend are supported")
    result = conn.execute(insert(table).values(**values).on_conflict_do_nothing())
    return bool(result.rowcount)


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, time.min, timezone.utc).timestamp() * 1000)


def _candle_date(row: list[str]) -> date:
    if len(row) != 9:
        raise ValueError("OKX candle must have nine fields")
    stamp = int(row[0])
    if stamp % UTC_DAY_MS:
        raise ValueError("OKX daily candle is not aligned to UTC midnight")
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).date()


def fetch_okx_candles(
    session: requests.Session, start: date, end: date
) -> list[list[str]]:
    """Page backwards from end, retaining only the requested UTC dates."""
    if start > end:
        return []
    cursor = _utc_ms(end + timedelta(days=1))
    start_ms = _utc_ms(start)
    result: list[list[str]] = []
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


def fetch_cbr_rates(
    session: requests.Session, start: date, end: date
) -> list[tuple[dict, str]]:
    """Use bounded date windows so the first historical load is reliable."""
    result: list[tuple[dict, str]] = []
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


def run_loader(
    engine: Engine, source: str, session: requests.Session | None = None,
    end: date | None = None,
) -> dict:
    """Store new complete days transactionally and log success or failure."""
    if source not in {"okx", "cbr"}:
        raise ValueError("source must be okx or cbr")
    session = session or build_session()
    end = end or (datetime.now(timezone.utc).date() - timedelta(days=1))
    started = datetime.now(timezone.utc)
    received = added = 0
    last_date = None
    status = "success"
    error = None
    table = raw_okx if source == "okx" else raw_cbr
    date_column = table.c.candle_date if source == "okx" else table.c.rate_date
    try:
        with engine.begin() as conn:
            prior = conn.execute(select(func.max(date_column))).scalar_one()
            first = FIRST_OKX_DATE if source == "okx" else FIRST_CBR_DATE
            start = prior + timedelta(days=1) if prior else first
            if start <= end and source == "okx":
                rows = fetch_okx_candles(session, start, end)
                received = len(rows)
                for row in rows:
                    candle_date = _candle_date(row)
                    if row[8] not in {"0", "1"}:
                        raise ValueError("Invalid OKX completion flag")
                    if row[8] == "0":
                        continue  # fetch the current candle again later
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
            elif start <= end:
                rows = fetch_cbr_rates(session, start, end)
                received = len(rows)
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
            last_date = conn.execute(select(func.max(date_column))).scalar_one()
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"[:2000]
        added = 0  # the transaction has been rolled back
    with engine.begin() as conn:
        conn.execute(load_log.insert().values(
            source=source, started_at=started, status=status,
            rows_received=received, rows_added=added,
            last_loaded_date=last_date, error_message=error,
        ))
    result = {
        "source": source, "status": status, "rows_received": received,
        "rows_added": added,
        "last_loaded_date": str(last_date) if last_date else None,
        "error_message": error,
    }
    if status == "failed":
        raise RuntimeError(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["okx", "cbr", "all"], default="all")
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    initialize_database(engine)
    sources = ["okx", "cbr"] if args.source == "all" else [args.source]
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
