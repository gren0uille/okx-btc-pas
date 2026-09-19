from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from okx_btc_pas.cleaning import (
    build_clean, clean_cbr, clean_okx, data_quality_log, validate_candle,
)
from okx_btc_pas.ingestion import raw_cbr, raw_okx

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


class Row:
    """Stands in for one raw candle when only validation is exercised."""

    def __init__(self, **values):
        defaults = {
            "open": Decimal("100"), "high": Decimal("110"),
            "low": Decimal("90"), "close": Decimal("105"),
            "volume_btc": Decimal("12.5"), "turnover_usdt": Decimal("1300"),
        }
        defaults.update(values)
        for key, value in defaults.items():
            setattr(self, key, value)


def add_candle(conn, day, **overrides):
    values = {
        "instrument_id": "BTC-USDT", "candle_date": date(2026, 9, day),
        "open": Decimal("100"), "high": Decimal("110"), "low": Decimal("90"),
        "close": Decimal("105"), "volume_btc": Decimal("12.5"),
        "turnover_usdt": Decimal("1300"), "confirm": 1,
        "source_json": "[]", "loaded_at": NOW,
    }
    values.update(overrides)
    conn.execute(raw_okx.insert().values(**values))


def test_valid_candle_passes():
    assert validate_candle(Row()) == []


def test_high_below_low_is_rejected():
    checks = [name for name, _, _ in validate_candle(Row(high=Decimal("80")))]
    assert "candle_high_low" in checks


def test_close_outside_range_is_rejected():
    checks = [name for name, _, _ in validate_candle(Row(close=Decimal("200")))]
    assert "candle_open_close_bounds" in checks


def test_negative_volume_is_rejected():
    checks = [name for name, _, _ in validate_candle(Row(volume_btc=Decimal("-1")))]
    assert "candle_volume_sign" in checks


def test_bad_row_is_logged_and_kept_out_of_clean(db):
    with db.begin() as conn:
        add_candle(conn, 1)
        add_candle(conn, 2, high=Decimal("50"))  # high below low

    stats = build_clean(db, now=NOW)

    assert stats["okx_rows"] == 1
    assert stats["okx_rejected"] == 1
    with db.begin() as conn:
        dates = [r[0] for r in conn.execute(select(clean_okx.c.candle_date))]
        assert dates == [date(2026, 9, 1)]
        failed = conn.execute(
            select(data_quality_log.c.check_name)
            .where(data_quality_log.c.passed.is_(False))
        ).scalars().all()
        assert "candle_high_low" in failed


def test_rate_is_divided_by_nominal(db):
    with db.begin() as conn:
        conn.execute(raw_cbr.insert().values(
            rate_date=date(2026, 9, 1), nominal=10,
            value=Decimal("800.0"), vunit_rate=Decimal("80.0"),
            source_xml="<Record/>", loaded_at=NOW,
        ))

    build_clean(db, now=NOW)

    with db.begin() as conn:
        rate = conn.execute(select(clean_cbr.c.rate_per_usd)).scalar_one()
        assert rate == Decimal("80")


def test_calendar_gap_is_reported(db):
    with db.begin() as conn:
        add_candle(conn, 1)
        add_candle(conn, 3)  # 2 September is missing

    stats = build_clean(db, now=NOW)

    assert stats["missing_days"] == 1
    with db.begin() as conn:
        entry = conn.execute(
            select(data_quality_log.c.details)
            .where(data_quality_log.c.check_name == "candle_calendar_continuity")
        ).scalar_one()
        assert "2026-09-02" in entry


def test_rebuild_is_idempotent(db):
    with db.begin() as conn:
        add_candle(conn, 1)
        add_candle(conn, 2)

    first = build_clean(db, now=NOW)
    second = build_clean(db, now=NOW)

    assert first == second
    with db.begin() as conn:
        assert conn.execute(select(func.count()).select_from(clean_okx)).scalar_one() == 2
