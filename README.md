# okx-btc-pas

Прогноз объёма торгов и волатильности BTC/USDT на OKX на следующие сутки.

## Запуск

```bash
cp .env.example .env      # задать пароли и ключ
docker compose up -d
```

Первый запуск загружает историю с 2018 года и строит дашборды (около 5 минут).
Superset: http://localhost:8088, логин и пароль — из `.env`.
Дальше конвейер запускается сам ежедневно в 00:30 UTC.

## Архитектура

```
OKX (JSON) ─────┐
ЦБ РФ (XML) ────┤                                     ┌─► модели ─► mart.forecast ─┐
alternative.me ─┼─► raw ─► clean ─► проверки ─► mart ─┤                            ├─► Superset
FRED (CSV) ─────┘    │       │      качества          └─► представления ◄──────────┘
                     └─► hist (версии значений)    meta: каталог, происхождение, журналы
```

| Слой | Назначение |
|---|---|
| raw | данные источников без изменений, с исходным ответом |
| clean | строки, прошедшие проверки; отбраковка пишется в журнал |
| mart | витрина признаков и целей, прогнозы, метрики моделей |
| hist | версии пересматриваемых значений (SCD Type 2) |
| meta | каталог наборов, происхождение данных, запуски, миграции |

## Стек

Python 3.13, PostgreSQL 16, SQLAlchemy, pandas, scikit-learn, Apache Superset 4.1, Docker Compose.

## Команды

```bash
docker compose run --rm pipeline python -m okx_btc_pas.pipeline      # запуск вручную
docker compose run --rm pipeline python -m okx_btc_pas.lineage \
  --metric volatility --date 2026-09-18                               # происхождение показателя
python -m pytest                                                      # тесты
```
