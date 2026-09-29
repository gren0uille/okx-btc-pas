# Историзация внешних факторов по схеме SCD Type 2
#
# Ставка ФРС и индекс страха и жадности могут пересматриваться задним
# числом. Загрузчик повторно запрашивает последние дни, и если значение
# за уже известную дату изменилось, прежняя версия не теряется:
# она закрывается (valid_to, is_current = false), а рядом появляется новая.
# Так можно ответить на вопрос «какое значение система видела на момент X»

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Integer, Numeric, String, Table, and_,
    select, update,
)

from .db import metadata

external_factor_version = Table(
    "external_factor_version", metadata,
    Column("factor_code", String(30), primary_key=True),
    Column("factor_date", Date, primary_key=True),
    Column("version_no", Integer, primary_key=True),
    Column("value", Numeric(20, 6)),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    Column("valid_to", DateTime(timezone=True)),
    Column("is_current", Boolean, nullable=False),
    Column("run_id", String(40)),
    schema="hist",
)


# Фиксируем значение фактора: новая дата — версия 1, изменённое значение —
# новая версия с закрытием прежней, то же самое значение — ничего не делаем
# Возвращает "new", "changed" или "same"
def record_version(conn, factor_code, factor_date, value, moment, run_id=None):
    table = external_factor_version
    current = conn.execute(select(table).where(and_(
        table.c.factor_code == factor_code,
        table.c.factor_date == factor_date,
        table.c.is_current.is_(True),
    ))).first()

    if current is not None and _same(current.value, value):
        return "same"

    version_no = 1
    if current is not None:
        conn.execute(update(table).where(and_(
            table.c.factor_code == factor_code,
            table.c.factor_date == factor_date,
            table.c.version_no == current.version_no,
        )).values(valid_to=moment, is_current=False))
        version_no = current.version_no + 1

    conn.execute(table.insert().values(
        factor_code=factor_code, factor_date=factor_date, version_no=version_no,
        value=value, valid_from=moment, valid_to=None, is_current=True,
        run_id=run_id,
    ))
    return "new" if current is None else "changed"


def _same(old, new):
    if old is None or new is None:
        return old is None and new is None
    return abs(float(old) - float(new)) < 1e-9


# Значение фактора, которое система считала актуальным на момент moment
def value_as_of(conn, factor_code, factor_date, moment):
    table = external_factor_version
    row = conn.execute(select(table.c.value).where(and_(
        table.c.factor_code == factor_code,
        table.c.factor_date == factor_date,
        table.c.valid_from <= moment,
        (table.c.valid_to.is_(None)) | (table.c.valid_to > moment),
    ))).first()
    return None if row is None else row.value
