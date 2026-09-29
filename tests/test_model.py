import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import func, select

from okx_btc_pas.cleaning import build_clean
from okx_btc_pas.ingestion import raw_cbr, raw_fng, raw_fred, raw_okx
from okx_btc_pas.mart import build_mart
from okx_btc_pas.model import (
    FEATURE_SETS, check_no_leakage, diebold_mariano, forecast, model_comparison,
    model_run, run_models,
)

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def _fill(db, start=date(2024, 1, 1), days=500):
    # Синтетический рынок: недельный цикл объёма и шум, чтобы модели
    # было что выучить, и все четыре источника без пропусков
    rng = np.random.default_rng(0)
    with db.begin() as conn:
        for i in range(days):
            day = start + timedelta(days=i)
            weekend = day.weekday() >= 5
            volume = 5000 * (0.6 if weekend else 1.0) * math.exp(rng.normal(0, 0.1))
            spread = 0.02 * math.exp(rng.normal(0, 0.2))
            conn.execute(raw_okx.insert().values(
                instrument_id="BTC-USDT", candle_date=day,
                open=Decimal("100"), high=Decimal(str(100 * (1 + spread))),
                low=Decimal("100"), close=Decimal("100"),
                volume_btc=Decimal(str(round(volume, 4))),
                turnover_usdt=Decimal("1"), confirm=1, source_json="[]", loaded_at=NOW))
            conn.execute(raw_cbr.insert().values(
                rate_date=day, nominal=1, value=Decimal("90"), vunit_rate=Decimal("90"),
                source_xml="", loaded_at=NOW))
            conn.execute(raw_fng.insert().values(
                index_date=day, value=int(rng.integers(10, 90)), classification="x",
                source_json="{}", loaded_at=NOW))
            conn.execute(raw_fred.insert().values(
                rate_date=day, series_id="DFF", value=Decimal("4.33"),
                source_line="", loaded_at=NOW))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)


def test_targets_are_never_features():
    for features in FEATURE_SETS.values():
        check_no_leakage(features)
    with pytest.raises(ValueError):
        check_no_leakage(["volume_btc", "target_volume_btc"])


def test_diebold_mariano_on_equal_and_different_errors():
    errors = np.random.default_rng(1).normal(0, 1, 300)
    assert diebold_mariano(errors, errors) == (0.0, 1.0)
    statistic, p_value = diebold_mariano(errors * 2, errors)
    assert statistic > 0 and p_value < 0.05


def test_models_run_end_to_end_and_respect_time_order(db):
    _fill(db)
    summary = run_models(db, run_id="test-run", now=NOW, test_start=date(2025, 3, 1))

    with db.connect() as conn:
        runs = conn.execute(select(model_run)).all()
        champions = [r for r in runs if r.is_champion]
        # По одному чемпиону на каждый показатель
        assert sorted(r.target for r in champions) == ["volatility", "volume"]
        # Обучение строго раньше проверки
        assert all(r.train_end < r.test_start for r in runs)
        # Прогноз на завтра — один на показатель, факт по нему ещё неизвестен
        future = conn.execute(select(forecast).where(
            forecast.c.is_backtest.is_(False))).all()
        assert len(future) == 2 and all(f.y_true is None for f in future)
        assert all(f.lower_80 <= f.y_pred <= f.upper_80 for f in future)
        assert conn.execute(select(func.count()).select_from(
            model_comparison)).scalar_one() == 4

    # Недельный цикл выучивается: обученная модель лучше «завтра как сегодня»
    volume = summary["volume"]["models"]
    assert volume["ridge/market"]["mae_vs_naive"] < 1


def test_previous_forecasts_are_kept(db):
    _fill(db)
    run_models(db, run_id="first", now=NOW, test_start=date(2025, 3, 1))
    run_models(db, run_id="second", now=NOW, test_start=date(2025, 3, 1))
    with db.connect() as conn:
        runs = conn.execute(select(forecast.c.run_id).distinct()).scalars().all()
    assert sorted(runs) == ["first", "second"]
