# Витрина mart: одна строка на сутки UTC с признаками и целевыми значениями
# В строке дня d лежат признаки, известные на конец этого дня, и цели,
# взятые из дня d+1. Модель на такой таблице предсказывает завтра по сегодня

import argparse
import bisect
import json
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    Column, Date, DateTime, Integer, Numeric, Table, create_engine, delete,
    select,
)
from .cleaning import clean_cbr, clean_fng, clean_fred, clean_okx
from .ingestion import initialize_database, metadata


# Оценка волатильности Паркинсона: |ln(high/low)| / (2*sqrt(ln 2))
PARKINSON_SCALE = 2 * math.sqrt(math.log(2))
LAGS = (1, 2, 3, 7)
WINDOWS = (7, 30)

daily_mart = Table(
    "daily_market_mart", metadata,
    Column("candle_date", Date, primary_key=True),

    # Признаки: всё ниже известно после закрытия суток candle_date
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

    # Внешние факторы. Для каждого берётся последнее значение, уже
    # опубликованное к концу суток candle_date, и хранится его возраст
    Column("usd_rub", Numeric(20, 6)),
    Column("usd_rub_age_days", Integer),
    Column("fear_greed", Integer),
    Column("fear_greed_age_days", Integer),
    Column("fed_rate", Numeric(10, 4)),
    Column("fed_rate_age_days", Integer),

    # Целевые значения: берутся из candle_date + 1, признаками не служат
    Column("target_volume_btc", Numeric(28, 8)),
    Column("target_volatility_pk", Numeric(20, 10)),

    Column("built_at", DateTime(timezone=True), nullable=False),
    schema="mart",
)


# Считаем дневную волатильность по максимуму и минимуму одной свечи
def parkinson(high, low):
    if high <= 0 or low <= 0:
        raise ValueError("Цены должны быть положительными")
    return Decimal(str(abs(math.log(float(high) / float(low))) / PARKINSON_SCALE))


def _mean(values):
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


# Ставка ФРС за день x публикуется на следующий рабочий день.
# Прогноз строится в конце суток d, поэтому ставку за d ещё не знаем
def fed_available_from(rate_date):
    day = rate_date + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


class AsOf:
    # Поиск последнего значения, доступного к концу суток day
    # Поиск идёт только назад: заглядывание вперёд дало бы модели
    # сведения, которых в момент прогноза не существует
    def __init__(self, pairs, available=lambda d: d):
        pairs = sorted(pairs)
        self.available = [available(d) for d, _ in pairs]
        self.dates = [d for d, _ in pairs]
        self.values = [v for _, v in pairs]
        # Дата доступности монотонна по дате значения, поэтому бинарный поиск
        # по available корректен

    def lookup(self, day):
        position = bisect.bisect_right(self.available, day) - 1
        if position < 0:
            return None, None
        return self.values[position], (day - self.dates[position]).days


# Перестраиваем витрину из слоя clean в одной транзакции
def build_mart(engine, now=None):
    moment = now or datetime.now(timezone.utc)

    initialize_database(engine)

    with engine.begin() as conn:
        candles = list(conn.execute(
            select(clean_okx.c.candle_date, clean_okx.c.high, clean_okx.c.low,
                   clean_okx.c.close, clean_okx.c.volume_btc)
            .order_by(clean_okx.c.candle_date)
        ))
        # Курс ЦБ устанавливается заранее и действует с указанной даты
        usd_rub = AsOf(conn.execute(
            select(clean_cbr.c.rate_date, clean_cbr.c.rate_per_usd)).all())
        # Индекс за сутки d публикуется в начале суток d
        fear_greed = AsOf(conn.execute(
            select(clean_fng.c.index_date, clean_fng.c.value)).all())
        fed_rate = AsOf(conn.execute(
            select(clean_fred.c.rate_date, clean_fred.c.rate_pct)).all(),
            available=fed_available_from)

        conn.execute(delete(daily_mart))
        stats = {"rows": 0, "rows_with_target": 0, "rows_with_rate": 0,
                 "rows_with_fear_greed": 0, "rows_with_fed_rate": 0}
        if not candles:
            return stats

        volumes = [row.volume_btc for row in candles]
        volatilities = [parkinson(row.high, row.low) for row in candles]
        by_date = {row.candle_date: i for i, row in enumerate(candles)}

        rows = []
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

            # Окно заканчивается предыдущим днём: включение сегодняшнего
            # значения дало бы среднему сведения о текущей строке
            for window in WINDOWS:
                start = max(0, i - window)
                values[f"volume_mean_{window}"] = _mean(volumes[start:i])
                values[f"volatility_mean_{window}"] = _mean(volatilities[start:i])

            values["usd_rub"], values["usd_rub_age_days"] = usd_rub.lookup(day)
            values["fear_greed"], values["fear_greed_age_days"] = fear_greed.lookup(day)
            values["fed_rate"], values["fed_rate_age_days"] = fed_rate.lookup(day)
            stats["rows_with_rate"] += int(values["usd_rub"] is not None)
            stats["rows_with_fear_greed"] += int(values["fear_greed"] is not None)
            stats["rows_with_fed_rate"] += int(values["fed_rate"] is not None)

            following = by_date.get(day + timedelta(days=1))
            if following is not None:
                values["target_volume_btc"] = candles[following].volume_btc
                values["target_volatility_pk"] = volatilities[following]
                stats["rows_with_target"] += 1
            else:
                values["target_volume_btc"] = None
                values["target_volatility_pk"] = None
            rows.append(values)

        conn.execute(daily_mart.insert(), rows)
        stats["rows"] = len(rows)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Построение витрины mart")
    parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    initialize_database(engine)
    print(json.dumps(build_mart(engine), ensure_ascii=False))


if __name__ == "__main__":
    main()
