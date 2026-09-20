"""Mart layer: one row per UTC day with features and forecast targets.

The row for day d holds features known once day d has closed, and targets
taken from day d+1. A model trained on this table therefore predicts the next
day from today's information only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    Column, Date, DateTime, Integer, Numeric, Table, create_engine, delete,
    select,
)
from sqlalchemy.engine import Engine

from .cleaning import clean_cbr, clean_okx
from .ingestion import initialize_database, metadata


# Parkinson's daily volatility estimator: |ln(high/low)| / (2*sqrt(ln 2)).
PARKINSON_SCALE = 2 * math.sqrt(math.log(2))
LAGS = (1, 2, 3, 7)
WINDOWS = (7, 30)

daily_mart = Table(
    "daily_market_mart", metadata,
    Column("candle_date", Date, primary_key=True),

    # Features: everything below is known once candle_date has closed.
    Column("volume_btc", Numeric(28, 8), nullable=False),
    Column("volatility_pk", Numeric(20, 10), nullable=False),
    Column("close", Numeric(24, 8), nullable=False),
    Column("log_return", Numeric(20, 10)),
    Column("day_of_week", Integer, nullable=False),
    Column("is_weekend", Integer, nullable=False),
    Column("month", Integer, nullable=False),

    Column("volume_lag_1", Numeric(28, 8)),
    Column("volume_lag_2", Numeric(28, 8)),
    Column("volume_lag_3", Numeric(28, 8)),
    Column("volume_lag_7", Numeric(28, 8)),
    Column("volatility_lag_1", Numeric(20, 10)),
    Column("volatility_lag_2", Numeric(20, 10)),
    Column("volatility_lag_3", Numeric(20, 10)),
    Column("volatility_lag_7", Numeric(20, 10)),
    Column("volume_mean_7", Numeric(28, 8)),
    Column("volume_mean_30", Numeric(28, 8)),
    Column("volatility_mean_7", Numeric(20, 10)),
    Column("volatility_mean_30", Numeric(20, 10)),

    # External factor: the latest CBR rate dated no later than candle_date.
    Column("usd_rub", Numeric(20, 6)),
    Column("usd_rub_age_days", Integer),

    # Targets: taken from candle_date + 1 and never used as features.
    Column("target_volume_btc", Numeric(28, 8)),
    Column("target_volatility_pk", Numeric(20, 10)),

    Column("built_at", DateTime(timezone=True), nullable=False),
    schema="mart",
)


def parkinson(high: Decimal, low: Decimal) -> Decimal:
    """Daily volatility from the high-low range of one candle."""
    if high <= 0 or low <= 0:
        raise ValueError("Цены должны быть положительными")
    return Decimal(str(abs(math.log(float(high) / float(low))) / PARKINSON_SCALE))


def _mean(values: list[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _rate_lookup(rates: list[tuple[date, Decimal]], day: date):
    """Latest rate dated no later than day, with its age in days.

    Looking forward would leak information the forecaster cannot have, so the
    search only ever walks backwards.
    """
    chosen = None
    for rate_date, value in rates:
        if rate_date <= day:
            chosen = (rate_date, value)
        else:
            break
    if chosen is None:
        return None, None
    return chosen[1], (day - chosen[0]).days


def build_mart(engine: Engine, now: datetime | None = None) -> dict:
    """Rebuild the mart from the clean layer inside one transaction."""
    moment = now or datetime.now(timezone.utc)

    initialize_database(engine)

    with engine.begin() as conn:
        candles = list(conn.execute(
            select(clean_okx.c.candle_date, clean_okx.c.high, clean_okx.c.low,
                   clean_okx.c.close, clean_okx.c.volume_btc)
            .order_by(clean_okx.c.candle_date)
        ))
        rates = [(r.rate_date, r.rate_per_usd) for r in conn.execute(
            select(clean_cbr.c.rate_date, clean_cbr.c.rate_per_usd)
            .order_by(clean_cbr.c.rate_date)
        )]

        conn.execute(delete(daily_mart))
        if not candles:
            return {"rows": 0, "rows_with_target": 0, "rows_with_rate": 0}

        volumes = [row.volume_btc for row in candles]
        volatilities = [parkinson(row.high, row.low) for row in candles]
        by_date = {row.candle_date: i for i, row in enumerate(candles)}

        rows = 0
        with_target = 0
        with_rate = 0
        for i, row in enumerate(candles):
            day = row.candle_date
            values = {
                "candle_date": day,
                "volume_btc": row.volume_btc,
                "volatility_pk": volatilities[i],
                "close": row.close,
                "day_of_week": day.weekday(),
                "is_weekend": int(day.weekday() >= 5),
                "month": day.month,
                "built_at": moment,
            }

            previous = candles[i - 1] if i else None
            values["log_return"] = (
                Decimal(str(math.log(float(row.close) / float(previous.close))))
                if previous is not None and previous.close > 0 and row.close > 0
                else None
            )

            for lag in LAGS:
                j = i - lag
                values[f"volume_lag_{lag}"] = volumes[j] if j >= 0 else None
                values[f"volatility_lag_{lag}"] = volatilities[j] if j >= 0 else None

            # Windows end at the previous day: including today's own value
            # would let the mean carry information about the current row.
            for window in WINDOWS:
                start = max(0, i - window)
                values[f"volume_mean_{window}"] = _mean(volumes[start:i])
                values[f"volatility_mean_{window}"] = _mean(volatilities[start:i])

            rate, age = _rate_lookup(rates, day)
            values["usd_rub"] = rate
            values["usd_rub_age_days"] = age
            with_rate += int(rate is not None)

            following = by_date.get(day + timedelta(days=1))
            if following is not None:
                values["target_volume_btc"] = candles[following].volume_btc
                values["target_volatility_pk"] = volatilities[following]
                with_target += 1
            else:
                values["target_volume_btc"] = None
                values["target_volatility_pk"] = None

            conn.execute(daily_mart.insert().values(**values))
            rows += 1

    return {"rows": rows, "rows_with_target": with_target,
            "rows_with_rate": with_rate}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    initialize_database(engine)
    print(json.dumps(build_mart(engine), ensure_ascii=False))


if __name__ == "__main__":
    main()
