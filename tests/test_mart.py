from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from okx_btc_pas.cleaning import build_clean
from okx_btc_pas.ingestion import raw_cbr, raw_okx
from okx_btc_pas.mart import build_mart, daily_mart, parkinson

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def add_candle(conn, day, volume, high=Decimal("110"), low=Decimal("90")):
    conn.execute(raw_okx.insert().values(
        instrument_id="BTC-USDT", candle_date=date(2026, 9, day),
        open=Decimal("100"), high=high, low=low, close=Decimal("105"),
        volume_btc=volume, turnover_usdt=Decimal("1300"), confirm=1,
        source_json="[]", loaded_at=NOW,
    ))


def row_for(db, day):
    with db.begin() as conn:
        return conn.execute(
            select(daily_mart).where(daily_mart.c.candle_date == date(2026, 9, day))
        ).one()


def test_parkinson_is_zero_for_flat_candle():
    assert parkinson(Decimal("100"), Decimal("100")) == Decimal("0")


def test_parkinson_grows_with_range():
    narrow = parkinson(Decimal("105"), Decimal("95"))
    wide = parkinson(Decimal("150"), Decimal("50"))
    assert wide > narrow > 0


def test_target_comes_from_the_next_day(db):
    with db.begin() as conn:
        add_candle(conn, 1, Decimal("10"))
        add_candle(conn, 2, Decimal("20"))
        add_candle(conn, 3, Decimal("30"))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    assert row_for(db, 1).target_volume_btc == Decimal("20")
    assert row_for(db, 2).target_volume_btc == Decimal("30")
    # The last day has no following day, so it carries no target.
    assert row_for(db, 3).target_volume_btc is None


def test_lags_and_windows_exclude_the_current_day(db):
    with db.begin() as conn:
        for day, volume in enumerate([10, 20, 30, 40], start=1):
            add_candle(conn, day, Decimal(volume))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    fourth = row_for(db, 4)
    assert fourth.volume_lag_1 == Decimal("30")
    assert fourth.volume_lag_2 == Decimal("20")
    # Mean over the three earlier days only: (10+20+30)/3, not including 40.
    assert fourth.volume_mean_7 == Decimal("20")


def test_no_target_leaks_into_features(db):
    # Признаки дня d по построению не должны совпадать со значениями дня d+1
    with db.begin() as conn:
        add_candle(conn, 1, Decimal("10"))
        add_candle(conn, 2, Decimal("999"))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    first = row_for(db, 1)
    feature_values = [
        first.volume_btc, first.volume_lag_1, first.volume_lag_2,
        first.volume_lag_3, first.volume_lag_7,
        first.volume_mean_7, first.volume_mean_30,
    ]
    assert Decimal("999") not in [v for v in feature_values if v is not None]
    assert first.target_volume_btc == Decimal("999")


def test_rate_is_carried_forward_never_backward(db):
    with db.begin() as conn:
        add_candle(conn, 5, Decimal("10"))
        add_candle(conn, 6, Decimal("11"))
        conn.execute(raw_cbr.insert().values(
            rate_date=date(2026, 9, 5), nominal=1, value=Decimal("90.5"),
            vunit_rate=Decimal("90.5"), source_xml="<Record/>", loaded_at=NOW,
        ))
        # A later rate must not reach the earlier day.
        conn.execute(raw_cbr.insert().values(
            rate_date=date(2026, 9, 6), nominal=1, value=Decimal("95.0"),
            vunit_rate=Decimal("95.0"), source_xml="<Record/>", loaded_at=NOW,
        ))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    assert row_for(db, 5).usd_rub == Decimal("90.5")
    assert row_for(db, 5).usd_rub_age_days == 0


def test_weekend_rate_keeps_its_age(db):
    with db.begin() as conn:
        # 5 September 2026 is a Saturday; the rate is dated Friday the 4th.
        add_candle(conn, 5, Decimal("10"))
        conn.execute(raw_cbr.insert().values(
            rate_date=date(2026, 9, 4), nominal=1, value=Decimal("90.0"),
            vunit_rate=Decimal("90.0"), source_xml="<Record/>", loaded_at=NOW,
        ))
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    row = row_for(db, 5)
    assert row.usd_rub == Decimal("90")
    assert row.usd_rub_age_days == 1


def test_calendar_features_are_correct(db):
    with db.begin() as conn:
        add_candle(conn, 5, Decimal("10"))  # Saturday
        add_candle(conn, 7, Decimal("10"))  # Monday
    build_clean(db, now=NOW)
    build_mart(db, now=NOW)

    assert row_for(db, 5).is_weekend == 1
    assert row_for(db, 7).is_weekend == 0
    assert row_for(db, 7).day_of_week == 0


def test_rebuild_is_idempotent(db):
    with db.begin() as conn:
        add_candle(conn, 1, Decimal("10"))
        add_candle(conn, 2, Decimal("20"))
    build_clean(db, now=NOW)

    first = build_mart(db, now=NOW)
    second = build_mart(db, now=NOW)

    assert first == second == {"rows": 2, "rows_with_target": 1, "rows_with_rate": 0}
