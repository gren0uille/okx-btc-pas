# okx-btc-pas

Прогноз объёма торгов и волатильности BTC/USDT на OKX.

## Запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest

cp .env.example .env          # задать пароль
docker compose up -d db
docker compose run --rm loader
docker compose run --rm loader python -m okx_btc_pas.cleaning
docker compose run --rm loader python -m okx_btc_pas.mart
```

## Состав

```
src/okx_btc_pas/
  source_probe.py   диагностика источников
  ingestion.py      загрузка в слой raw
  cleaning.py       слой clean, проверки качества
  mart.py           витрина mart
tests/              автоматические тесты
docs/               постановка задачи
```

Источники: OKX (суточные свечи BTC/USDT) и Банк России (курс USD/RUB).
СУБД: PostgreSQL 16 в Docker, схемы `raw`, `clean`, `mart`.
