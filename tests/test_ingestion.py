from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from okx_btc_pas.ingestion import (
    fetch_okx_candles, load_log, raw_cbr, raw_okx, run_loader,
)


def stamp(day):
    return str(int(datetime(2018, 1, day, tzinfo=timezone.utc).timestamp() * 1000))


def candle(day, confirm="1"):
    return [stamp(day), "100", "110", "90", "105", "12.5", "1300", "1300", confirm]


class FakeResponse:
    def __init__(self, data=None, content=b""):
        self.data = data
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, fail=False, last_confirm="1"):
        self.fail = fail
        self.last_confirm = last_confirm
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        if self.fail:
            raise ConnectionError("temporary source outage")
        if "history-candles" in url:
            upper = int(params["after"])
            rows = [candle(13, self.last_confirm), candle(12), candle(11)]
            rows = [row for row in rows if int(row[0]) < upper]
            return FakeResponse({"code": "0", "data": rows[:300]})
        if "XML_dynamic" in url:
            records = ""
            for day in (11, 12):
                candidate = date(2018, 1, day)
                start = datetime.strptime(params["date_req1"], "%d/%m/%Y").date()
                end = datetime.strptime(params["date_req2"], "%d/%m/%Y").date()
                if start <= candidate <= end:
                    records += (
                        f"<Record Date='{day:02d}.01.2018' Id='R01235'>"
                        "<Nominal>1</Nominal><Value>56,0000</Value>"
                        "<VunitRate>56,0000</VunitRate></Record>"
                    )
            return FakeResponse(content=f"<ValCurs ID='R01235'>{records}</ValCurs>".encode())
        raise AssertionError(f"Unexpected URL: {url}")


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_okx_pagination_and_date_filter():
    rows = fetch_okx_candles(FakeSession(), date(2018, 1, 11), date(2018, 1, 12))
    assert [row[0] for row in rows] == [stamp(12), stamp(11)]


def test_repeat_run_adds_no_duplicates(db):
    session = FakeSession()
    first_okx = run_loader(db, "okx", session, date(2018, 1, 12))
    first_cbr = run_loader(db, "cbr", session, date(2018, 1, 12))
    second_okx = run_loader(db, "okx", session, date(2018, 1, 12))
    second_cbr = run_loader(db, "cbr", session, date(2018, 1, 12))
    assert (first_okx["rows_added"], first_cbr["rows_added"]) == (2, 2)
    assert (second_okx["rows_added"], second_cbr["rows_added"]) == (0, 0)
    assert count(db, raw_okx) == count(db, raw_cbr) == 2
    assert count(db, load_log) == 4


def test_unfinished_candle_is_retried_later(db):
    session = FakeSession(last_confirm="0")
    first = run_loader(db, "okx", session, date(2018, 1, 13))
    assert (first["rows_received"], first["rows_added"]) == (3, 2)
    assert count(db, raw_okx) == 2
    session.last_confirm = "1"
    second = run_loader(db, "okx", session, date(2018, 1, 13))
    assert second["rows_added"] == 1
    assert count(db, raw_okx) == 3


def test_source_failure_is_logged_without_partial_rows(db):
    with pytest.raises(RuntimeError):
        run_loader(db, "okx", FakeSession(fail=True), date(2018, 1, 12))
    assert count(db, raw_okx) == 0
    with db.connect() as conn:
        entry = conn.execute(select(load_log)).mappings().one()
    assert entry["status"] == "failed"
    assert "temporary source outage" in entry["error_message"]
