# Контроль качества на уровне наборов данных
#
# Построчные проверки выполняются при построении clean (cleaning.py) и
# отбраковывают отдельные строки. Здесь проверяется набор целиком — то, что
# по одной строке не увидеть: свежесть, дубликаты, сходимость количества
# строк между слоями, ссылочная целостность, соответствие типов исходнику.
#
# Поведение системы задаётся критичностью:
#   warning  — записать в журнал и продолжить
#   critical — записать и остановить конвейер до построения прогноза,
#              чтобы не выдать пользователю прогноз по испорченным данным

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from xml.etree import ElementTree

from sqlalchemy import func, select

from .cleaning import (
    clean_cbr, clean_fng, clean_fred, clean_okx, data_quality_log, log_check,
)
from .ingestion import raw_cbr, raw_fng, raw_fred, raw_okx
from .mart import daily_mart

# Допустимое отставание последней даты от вчерашнего дня. ЦБ не публикует
# курс в выходные и праздники, самый длинный перерыв — новогодний
FRESHNESS_DAYS = {"okx": 1, "fng": 1, "fred": 4, "cbr": 12}
# Биржевые данные — основа обоих показателей: без них прогноз невозможен
CRITICAL_SOURCES = {"okx"}
MAX_REJECT_SHARE = 0.01
MAX_EXTERNAL_NULL_SHARE = 0.05

_RAW = {"okx": (raw_okx, raw_okx.c.candle_date), "cbr": (raw_cbr, raw_cbr.c.rate_date),
        "fng": (raw_fng, raw_fng.c.index_date), "fred": (raw_fred, raw_fred.c.rate_date)}
_CLEAN = {"okx": (clean_okx, clean_okx.c.candle_date),
          "cbr": (clean_cbr, clean_cbr.c.rate_date),
          "fng": (clean_fng, clean_fng.c.index_date),
          "fred": (clean_fred, clean_fred.c.rate_date)}


class Checker:
    def __init__(self, conn, moment, run_id):
        self.conn, self.moment, self.run_id = conn, moment, run_id
        self.results = []

    def record(self, table, check, check_type, passed, details, severity):
        severity = "info" if passed else severity
        action = ("log_only" if passed or severity == "warning" else "stop_pipeline")
        log_check(self.conn, self.moment, table, check, check_type, None, passed,
                  details, severity=severity, action=action, layer="quality",
                  run_id=self.run_id)
        self.results.append({"table": table, "check": check, "passed": passed,
                             "severity": severity, "details": details})

    def count(self, table):
        return self.conn.execute(select(func.count()).select_from(table)).scalar_one()


# Актуальность: последняя загруженная дата не старше допустимого
def check_freshness(ck, today):
    for source, (table, column) in _RAW.items():
        last = ck.conn.execute(select(func.max(column))).scalar_one()
        expected = today - timedelta(days=1)
        lag = None if last is None else (expected - last).days
        passed = lag is not None and lag <= FRESHNESS_DAYS[source]
        ck.record(table.fullname, "freshness", "timeliness", passed,
                  f"последняя дата {last}, отставание {lag} дн., "
                  f"допустимо {FRESHNESS_DAYS[source]}",
                  "critical" if source in CRITICAL_SOURCES else "warning")


# Уникальность: в каждом слое одна запись на дату
def check_uniqueness(ck):
    tables = [(t, c) for t, c in list(_RAW.values()) + list(_CLEAN.values())]
    tables.append((daily_mart, daily_mart.c.candle_date))
    for table, column in tables:
        duplicates = ck.conn.execute(
            select(func.count()).select_from(
                select(column).group_by(column).having(func.count() > 1).subquery())
        ).scalar_one()
        ck.record(table.fullname, "unique_date", "uniqueness", duplicates == 0,
                  f"дат с повторами: {duplicates}", "critical")


# Сходимость количества строк: всё, что есть в raw, либо принято в clean,
# либо отбраковано с записью в журнал. Расхождение значит, что строки
# потерялись без следа
def check_reconciliation(ck):
    for source in _RAW:
        raw_table, _ = _RAW[source]
        clean_table, _ = _CLEAN[source]
        raw_rows, clean_rows = ck.count(raw_table), ck.count(clean_table)
        rejected = raw_rows - clean_rows
        share = rejected / raw_rows if raw_rows else 0
        ck.record(clean_table.fullname, "reject_share", "validity",
                  share <= MAX_REJECT_SHARE,
                  f"raw {raw_rows}, clean {clean_rows}, отбраковано {rejected} "
                  f"({share:.2%}), допустимо {MAX_REJECT_SHARE:.0%}",
                  "critical" if source in CRITICAL_SOURCES else "warning")


# Ссылочная целостность: каждая строка слоя опирается на строку предыдущего
def check_references(ck):
    pairs = [
        (clean_okx.c.candle_date, raw_okx.c.candle_date, clean_okx, raw_okx),
        (daily_mart.c.candle_date, clean_okx.c.candle_date, daily_mart, clean_okx),
    ]
    for child_column, parent_column, child, parent in pairs:
        orphans = ck.conn.execute(
            select(func.count()).select_from(child).where(
                child_column.not_in(select(parent_column)))).scalar_one()
        ck.record(child.fullname, f"references_{parent.fullname}",
                  "referential_integrity", orphans == 0,
                  f"строк без соответствия в {parent.fullname}: {orphans}", "critical")


# Соответствие типов: типизированные столбцы raw совпадают с исходным
# ответом источника, сохранённым рядом. Расхождение значит ошибку разбора
def check_types(ck):
    mismatches = 0
    rows = ck.conn.execute(select(raw_okx.c.candle_date, raw_okx.c.volume_btc,
                                  raw_okx.c.high, raw_okx.c.low,
                                  raw_okx.c.source_json)).all()
    for row in rows:
        source = json.loads(row.source_json)
        if (Decimal(source[5]) != row.volume_btc or Decimal(source[2]) != row.high
                or Decimal(source[3]) != row.low):
            mismatches += 1
    ck.record(raw_okx.fullname, "typed_matches_source", "type_conformity",
              mismatches == 0, f"проверено {len(rows)}, расхождений {mismatches}",
              "critical")

    mismatches = 0
    rows = ck.conn.execute(select(raw_cbr.c.value, raw_cbr.c.source_xml)).all()
    for row in rows:
        node = ElementTree.fromstring(row.source_xml)
        if Decimal(node.findtext("Value").replace(",", ".")) != row.value:
            mismatches += 1
    ck.record(raw_cbr.fullname, "typed_matches_source", "type_conformity",
              mismatches == 0, f"проверено {len(rows)}, расхождений {mismatches}",
              "warning")

    mismatches = 0
    rows = ck.conn.execute(select(raw_fng.c.value, raw_fng.c.source_json)).all()
    for row in rows:
        value = json.loads(row.source_json).get("value")
        if (int(value) if value not in (None, "") else None) != row.value:
            mismatches += 1
    ck.record(raw_fng.fullname, "typed_matches_source", "type_conformity",
              mismatches == 0, f"проверено {len(rows)}, расхождений {mismatches}",
              "warning")


# Диапазоны и полнота витрины
def check_mart(ck, today):
    negative = ck.conn.execute(select(func.count()).select_from(daily_mart).where(
        (daily_mart.c.volume_btc < 0) | (daily_mart.c.volatility_pk < 0))).scalar_one()
    ck.record(daily_mart.fullname, "non_negative_targets", "range", negative == 0,
              f"строк с отрицательным объёмом или волатильностью: {negative}",
              "critical")

    since = today - timedelta(days=365)
    recent = ck.conn.execute(select(func.count()).select_from(daily_mart).where(
        daily_mart.c.candle_date >= since)).scalar_one()
    for column in ("usd_rub", "fear_greed", "fed_rate"):
        nulls = ck.conn.execute(select(func.count()).select_from(daily_mart).where(
            daily_mart.c.candle_date >= since,
            getattr(daily_mart.c, column).is_(None))).scalar_one()
        share = nulls / recent if recent else 1
        ck.record(daily_mart.fullname, f"completeness_{column}", "completeness",
                  share <= MAX_EXTERNAL_NULL_SHARE,
                  f"пропусков за последний год: {nulls} из {recent} ({share:.1%})",
                  "warning")


# Проверки после построения clean: решают, можно ли строить витрину
def run_source_checks(engine, run_id=None, now=None):
    moment = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        ck = Checker(conn, moment, run_id)
        check_freshness(ck, moment.date())
        check_uniqueness(ck)
        check_reconciliation(ck)
        check_types(ck)
        check_references(ck)
    return _summary(ck)


# Проверки после построения витрины: решают, можно ли строить прогноз
def run_mart_checks(engine, run_id=None, now=None):
    moment = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        ck = Checker(conn, moment, run_id)
        check_mart(ck, moment.date())
        check_references(ck)
    return _summary(ck)


def _summary(ck):
    failed = [r for r in ck.results if not r["passed"]]
    return {
        "checks": len(ck.results),
        "failed": len(failed),
        "critical": [f"{r['table']}: {r['check']} — {r['details']}"
                     for r in failed if r["severity"] == "critical"],
        "warnings": [f"{r['table']}: {r['check']} — {r['details']}"
                     for r in failed if r["severity"] == "warning"],
    }


def latest_results(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(data_quality_log).where(
            data_quality_log.c.run_id == run_id,
            data_quality_log.c.layer == "quality")).all()
