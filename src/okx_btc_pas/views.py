# Представления для дашбордов Superset
#
# Дашборды читают представления, а не таблицы: представление всегда отражает
# текущее состояние витрины, поэтому после очередного запуска конвейера
# графики обновляются без ручной синхронизации. Каждое представление
# отвечает на конкретный вопрос пользователя (указан в комментарии)
#
# Superset подключается ролью bi_reader, у которой есть только чтение
# схем mart и meta: из дашборда нельзя изменить или удалить данные

from sqlalchemy import text

VIEWS = {
    # Предметный дашборд -----------------------------------------------------

    # Как менялись объём и волатильность, каков обычный уровень?
    "mart.v_market_daily": """
        SELECT m.candle_date,
               m.volume_btc::float AS volume_btc,
               m.volatility_pk::float AS volatility_pk,
               m.close::float AS close_usdt,
               avg(m.volume_btc) OVER w30::float AS volume_ma30,
               avg(m.volatility_pk) OVER w30::float AS volatility_ma30,
               m.fear_greed,
               f.classification AS fear_greed_class,
               m.fed_rate::float AS fed_rate,
               m.usd_rub::float AS usd_rub,
               extract(year FROM m.candle_date)::int AS year,
               extract(isodow FROM m.candle_date)::int AS weekday_num,
               CASE extract(isodow FROM m.candle_date)::int
                    WHEN 1 THEN '1 Пн' WHEN 2 THEN '2 Вт' WHEN 3 THEN '3 Ср'
                    WHEN 4 THEN '4 Чт' WHEN 5 THEN '5 Пт' WHEN 6 THEN '6 Сб'
                    ELSE '7 Вс' END AS weekday
        FROM mart.daily_market_mart m
        LEFT JOIN clean.fear_greed_index f ON f.index_date = m.candle_date
        WINDOW w30 AS (ORDER BY m.candle_date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
    """,

    # Насколько прогноз модели-чемпиона совпадал с фактом?
    "mart.v_forecast_vs_actual": """
        WITH latest AS (SELECT max(run_id) AS run_id FROM mart.model_run),
             champion AS (
                SELECT r.target, r.model_name, r.feature_set FROM mart.model_run r
                JOIN latest USING (run_id) WHERE r.is_champion)
        SELECT f.target,
               CASE f.target WHEN 'volume' THEN 'Объём, BTC'
                             ELSE 'Волатильность' END AS target_name,
               f.target_date, f.y_pred, f.lower_80, f.upper_80,
               COALESCE(f.y_true, CASE f.target WHEN 'volume' THEN m.volume_btc
                                             ELSE m.volatility_pk END::float) AS y_true,
               f.is_backtest,
               f.model_name || ' / ' || f.feature_set AS model
        FROM mart.forecast f
        JOIN latest USING (run_id)
        JOIN champion USING (target, model_name, feature_set)
        LEFT JOIN mart.daily_market_mart m ON m.candle_date = f.target_date
    """,

    # Что ожидать завтра и насколько это необычно по сравнению с месяцем?
    "mart.v_next_day_forecast": """
        WITH latest AS (SELECT max(run_id) AS run_id FROM mart.model_run),
             recent AS (
                SELECT avg(volume_btc)::float AS volume_ma30,
                       avg(volatility_pk)::float AS volatility_ma30
                FROM (SELECT * FROM mart.daily_market_mart
                      ORDER BY candle_date DESC LIMIT 30) t)
        SELECT f.target,
               CASE f.target WHEN 'volume' THEN 'Объём, BTC'
                             ELSE 'Волатильность' END AS target_name,
               f.target_date, f.y_pred, f.lower_80, f.upper_80,
               f.model_name || ' / ' || f.feature_set AS model,
               CASE f.target WHEN 'volume' THEN r.volume_ma30
                             ELSE r.volatility_ma30 END AS typical_30d,
               round((100 * (f.y_pred / NULLIF(CASE f.target WHEN 'volume'
                    THEN r.volume_ma30 ELSE r.volatility_ma30 END, 0) - 1))::numeric, 1)
                    AS deviation_pct
        FROM mart.forecast f JOIN latest USING (run_id) CROSS JOIN recent r
        WHERE NOT f.is_backtest
    """,

    # Насколько можно доверять прогнозу и лучше ли модель простого правила?
    "mart.v_model_quality": """
        SELECT r.target,
               CASE r.target WHEN 'volume' THEN 'Объём, BTC'
                             ELSE 'Волатильность' END AS target_name,
               r.model_name || ' / ' || r.feature_set AS model,
               round(r.cv_mae_mean::numeric, 6) AS cv_mae,
               round(r.mae::numeric, 6) AS test_mae,
               round(r.mape::numeric, 1) AS mape_pct,
               round(r.r2::numeric, 3) AS r2,
               round(r.mae_vs_naive::numeric, 3) AS mae_vs_naive,
               round(r.coverage_80::numeric, 3) AS coverage_80,
               r.is_champion, r.test_start, r.test_end, r.run_id
        FROM mart.model_run r
        WHERE r.run_id = (SELECT max(run_id) FROM mart.model_run)
    """,

    # Помогают ли внешние факторы (курс ЦБ, индекс страха, ставка ФРС)?
    "mart.v_external_factor_test": """
        SELECT CASE target WHEN 'volume' THEN 'Объём, BTC'
                           ELSE 'Волатильность' END AS target_name,
               model_name,
               round(mae_market::numeric, 6) AS mae_without_external,
               round(mae_external::numeric, 6) AS mae_with_external,
               round(p_value::numeric, 4) AS p_value, verdict
        FROM mart.model_comparison
        WHERE run_id = (SELECT max(run_id) FROM mart.model_run)
    """,

    # Какие признаки сильнее всего влияют на прогноз чемпиона?
    "mart.v_feature_importance": """
        SELECT i.target,
               CASE i.target WHEN 'volume' THEN 'Объём, BTC'
                             ELSE 'Волатильность' END AS target_name,
               i.feature, i.importance
        FROM mart.feature_importance i
        JOIN mart.model_run r USING (run_id, target, model_name, feature_set)
        WHERE r.is_champion AND i.run_id = (SELECT max(run_id) FROM mart.model_run)
    """,

    # Операционный дашборд ---------------------------------------------------

    # Все ли источники загружаются вовремя?
    "meta.v_load_status": """
        WITH last AS (
            SELECT DISTINCT ON (source) source, started_at, status,
                   last_loaded_date, error_message
            FROM load_log ORDER BY source, started_at DESC),
             ok AS (SELECT source, max(started_at) AS last_success
                    FROM load_log WHERE status = 'success' GROUP BY source)
        SELECT l.source, l.started_at AS last_run, l.status AS last_status,
               ok.last_success, l.last_loaded_date,
               (current_date - 1 - l.last_loaded_date) AS days_behind,
               l.error_message
        FROM last l LEFT JOIN ok USING (source)
    """,

    # Сколько данных приходит и бывают ли сбои?
    "meta.v_load_history": """
        SELECT started_at::date AS run_date, source,
               count(*) AS runs,
               sum(rows_added) AS rows_added,
               sum(COALESCE(rows_updated, 0)) AS rows_updated,
               sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failures
        FROM load_log GROUP BY 1, 2
    """,

    # Прошли ли проверки качества в последнем запуске?
    "meta.v_quality_latest": """
        SELECT q.table_name, q.check_name, q.check_type, q.passed,
               q.severity, q.action, q.details, q.checked_at
        FROM data_quality_log q
        WHERE q.layer = 'quality'
          AND q.run_id = (SELECT run_id FROM data_quality_log
                          WHERE layer = 'quality' AND run_id IS NOT NULL
                          ORDER BY checked_at DESC LIMIT 1)
    """,

    # Как часто и где обнаруживаются проблемы качества?
    "meta.v_quality_history": """
        SELECT checked_at::date AS check_date, layer, check_type, severity,
               count(*) AS checks,
               sum(CASE WHEN passed THEN 0 ELSE 1 END) AS failed
        FROM data_quality_log GROUP BY 1, 2, 3, 4
    """,

    # Отрабатывает ли конвейер целиком и сколько времени занимает?
    "meta.v_pipeline_runs": """
        SELECT run_id, started_at, finished_at, status,
               round(extract(epoch FROM finished_at - started_at)::numeric, 1)
                   AS duration_sec,
               error_message
        FROM meta.pipeline_run
    """,

    # Пересматривали ли источники уже загруженные значения?
    "meta.v_factor_revisions": """
        SELECT factor_code, factor_date, version_no, value::float AS value,
               valid_from, valid_to, is_current
        FROM hist.external_factor_version
        WHERE (factor_code, factor_date) IN (
            SELECT factor_code, factor_date FROM hist.external_factor_version
            GROUP BY 1, 2 HAVING count(*) > 1)
    """,

    # Откуда берётся показатель на дашборде?
    "meta.v_lineage": """
        SELECT e.edge_id, e.from_dataset, e.from_column, e.to_dataset, e.to_column,
               e.transformation, e.process, d.layer AS to_layer
        FROM meta.lineage_edge e LEFT JOIN meta.dataset d
             ON d.dataset_name = e.to_dataset
    """,
}


# Пересоздаём представления. Работает только на PostgreSQL: в тестовой
# SQLite нет части используемых функций (DISTINCT ON, ::)
def create_views(engine):
    if engine.dialect.name != "postgresql":
        return 0
    with engine.begin() as conn:
        for name, sql in VIEWS.items():
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(f"CREATE VIEW {name} AS {sql}"))
    return len(VIEWS)


# Роль только для чтения, которой подключается Superset
def grant_bi_reader(engine, password):
    if engine.dialect.name != "postgresql" or not password:
        return False
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM pg_roles WHERE rolname = 'bi_reader'")).first()
        # Пароль передаётся литералом: ALTER/CREATE ROLE не принимает параметры
        quoted = password.replace("'", "''")
        verb = "ALTER" if exists else "CREATE"
        conn.execute(text(f"{verb} ROLE bi_reader WITH LOGIN PASSWORD '{quoted}'"))
        for schema in ("mart", "meta"):
            conn.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO bi_reader"))
            conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO bi_reader"))
            # Представления пересоздаются при каждом запуске, а права на
            # удалённый объект на новый не переходят. Права по умолчанию
            # выдают чтение на всё, что будет создано в схеме позже
            conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                              f"GRANT SELECT ON TABLES TO bi_reader"))
    return True
