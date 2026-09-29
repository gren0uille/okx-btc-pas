# Автоматическая настройка Superset: подключение, наборы данных, графики,
# предметный и операционный дашборды
#
# Запускается один раз после старта Superset (сервис superset-bootstrap).
# Повторный запуск удаляет созданные им графики и дашборды и создаёт заново,
# поэтому описание дашбордов живёт в коде, а не только в базе Superset
#
# Каждый график отвечает на один вопрос пользователя, вопрос записан в
# описании графика и виден при наведении на заголовок

import json
import os
import sys
import time

import requests
from sqlalchemy import create_engine, inspect

BASE = os.environ.get("SUPERSET_URL", "http://localhost:8088")
DB_NAME = "PAS: витрины (только чтение)"
MARKET_SLUG = "pas-market"
OPS_SLUG = "pas-operations"


class Api:
    def __init__(self):
        self.s = requests.Session()
        for attempt in range(30):
            try:
                r = self.s.post(f"{BASE}/api/v1/security/login", json={
                    "username": os.environ["SUPERSET_ADMIN_USER"],
                    "password": os.environ["SUPERSET_ADMIN_PASSWORD"],
                    "provider": "db", "refresh": True}, timeout=30)
                if r.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(5)
        else:
            sys.exit("Superset недоступен")
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        token = self.s.get(f"{BASE}/api/v1/security/csrf_token/", timeout=30)
        self.s.headers["X-CSRFToken"] = token.json()["result"]
        self.s.headers["Referer"] = BASE

    # Superset ограничивает частоту запросов к API (50 в секунду), поэтому
    # между вызовами выдерживается пауза, а ответ 429 повторяется
    def call(self, method, path, **kwargs):
        for attempt in range(5):
            time.sleep(0.05)
            r = self.s.request(method, f"{BASE}{path}", timeout=120, **kwargs)
            if r.status_code != 429:
                break
            time.sleep(2 * (attempt + 1))
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:500]}")
        return r.json() if r.content else {}

    def list(self, resource):
        return self.call("GET", f"/api/v1/{resource}/?q=(page_size:1000)")["result"]


# Ждём, пока конвейер создаст представления: при первом развёртывании
# Superset стартует быстрее, чем проходит первая загрузка данных
def wait_for_views(required, timeout=1800):
    engine = create_engine(os.environ["DATABASE_URL"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            names = {f"{schema}.{view}" for schema in ("mart", "meta")
                     for view in inspect(engine).get_view_names(schema=schema)}
            if required <= names:
                return
        except Exception:
            pass
        print("ожидание представлений от конвейера...", flush=True)
        time.sleep(15)
    sys.exit("Представления не появились: проверьте сервис pipeline")


def ensure_database(api):
    uri = (f"postgresql+psycopg2://bi_reader:{os.environ['BI_READER_PASSWORD']}"
           f"@db:5432/{os.environ.get('POSTGRES_DB', 'pas')}")
    payload = {"database_name": DB_NAME, "sqlalchemy_uri": uri,
               "expose_in_sqllab": True, "allow_run_async": False}
    for db in api.list("database"):
        if db["database_name"] == DB_NAME:
            api.call("PUT", f"/api/v1/database/{db['id']}", json=payload)
            return db["id"]
    return api.call("POST", "/api/v1/database/", json=payload)["id"]


# Подписи столбцов на русском: в таблицах дашборда видны они, а не имена в базе
LABELS = {
    "target_name": "Показатель", "target_date": "Дата прогноза", "y_pred": "Прогноз",
    "y_true": "Факт", "lower_80": "Нижняя граница 80 %",
    "upper_80": "Верхняя граница 80 %", "typical_30d": "Среднее за 30 дней",
    "deviation_pct": "Отклонение от среднего, %", "model": "Модель",
    "model_name": "Модель", "cv_mae": "MAE скользящей проверки",
    "test_mae": "MAE отложенной выборки", "mape_pct": "MAPE, %", "r2": "R²",
    "mae_vs_naive": "MAE / MAE «завтра как сегодня»",
    "coverage_80": "Попадание в интервал 80 %", "is_champion": "Чемпион",
    "mae_without_external": "MAE без внешних факторов",
    "mae_with_external": "MAE с внешними факторами", "p_value": "p-значение",
    "verdict": "Вывод", "source": "Источник", "last_status": "Статус",
    "last_run": "Последний запуск", "last_success": "Последний успех",
    "last_loaded_date": "Последняя дата данных", "days_behind": "Отставание, дн.",
    "error_message": "Ошибка", "run_id": "Запуск", "status": "Статус",
    "started_at": "Начало", "duration_sec": "Длительность, с",
    "check_type": "Тип проверки", "table_name": "Таблица", "check_name": "Проверка",
    "passed": "Пройдена", "severity": "Критичность", "action": "Действие системы",
    "details": "Подробности", "factor_code": "Фактор", "factor_date": "Дата значения",
    "version_no": "Версия", "value": "Значение", "valid_from": "Действует с",
    "valid_to": "Действует по", "is_current": "Текущая",
    "from_dataset": "Из набора", "from_column": "Из столбца", "to_dataset": "В набор",
    "to_column": "В столбец", "transformation": "Преобразование", "process": "Процесс",
    "weekday": "День недели", "year": "Год", "fear_greed_band": "Настроение рынка",
    "feature": "Признак", "run_date": "Дата", "check_date": "Дата",
}
COLUMN_KEYS = ("id", "column_name", "verbose_name", "type", "is_dttm", "groupby",
               "filterable", "expression", "description", "python_date_format",
               "is_active", "extra", "advanced_data_type")


def set_labels(api, dataset_id):
    columns = api.call("GET", f"/api/v1/dataset/{dataset_id}")["result"]["columns"]
    payload = []
    for column in columns:
        item = {k: column[k] for k in COLUMN_KEYS if k in column}
        item["verbose_name"] = LABELS.get(column["column_name"], column.get("verbose_name"))
        payload.append(item)
    api.call("PUT", f"/api/v1/dataset/{dataset_id}", json={"columns": payload})


def ensure_dataset(api, database_id, schema, table, time_column=None):
    existing = {(d["schema"], d["table_name"]): d["id"] for d in api.list("dataset")}
    dataset_id = existing.get((schema, table))
    if dataset_id is None:
        dataset_id = api.call("POST", "/api/v1/dataset/", json={
            "database": database_id, "schema": schema, "table_name": table})["id"]
    # Столбцы перечитываются из базы: представление могло измениться
    api.call("PUT", f"/api/v1/dataset/{dataset_id}/refresh")
    if time_column:
        api.call("PUT", f"/api/v1/dataset/{dataset_id}",
                 json={"main_dttm_col": time_column})
    set_labels(api, dataset_id)
    return dataset_id


def metric(expression, label):
    return {"expressionType": "SQL", "sqlExpression": expression, "label": label}


def where(expression):
    return {"expressionType": "SQL", "sqlExpression": expression, "clause": "WHERE"}


def period_filter(column):
    return {"expressionType": "SIMPLE", "subject": column, "operator": "TEMPORAL_RANGE",
            "comparator": "No filter", "clause": "WHERE"}


# Описания графиков ----------------------------------------------------------

def line(x, metrics, filters=(), **extra):
    return {"viz_type": "echarts_timeseries_line", "x_axis": x, "time_grain_sqla": "P1D",
            "metrics": metrics, "groupby": [], "row_limit": 10000,
            "adhoc_filters": [period_filter(x), *filters], "time_range": "No filter",
            "show_legend": True, "legendType": "scroll", "legendOrientation": "top",
            "rich_tooltip": True, "zoomable": True, "markerEnabled": False,
            "y_axis_format": "SMART_NUMBER", "x_axis_time_format": "smart_date",
            "truncate_metric": True, **extra}


def bar(x, metrics, filters=(), groupby=(), temporal=False, **extra):
    params = {"viz_type": "echarts_timeseries_bar", "x_axis": x, "metrics": metrics,
              "groupby": list(groupby), "row_limit": 10000,
              "adhoc_filters": ([period_filter(x)] if temporal else []) + list(filters),
              "time_range": "No filter", "show_legend": bool(groupby),
              "legendOrientation": "top", "rich_tooltip": True,
              "y_axis_format": "SMART_NUMBER", "truncate_metric": True,
              "x_axis_sort_asc": True, **extra}
    if temporal:
        params["time_grain_sqla"] = "P1D"
    return params


def number(expression, label, filters=(), fmt="SMART_NUMBER", subheader=""):
    return {"viz_type": "big_number_total", "metric": metric(expression, label),
            "adhoc_filters": list(filters), "subheader": subheader,
            "y_axis_format": fmt, "header_font_size": 0.4, "subheader_font_size": 0.15}


def table(columns, filters=(), order=None, limit=1000, formats=None):
    return {"viz_type": "table", "query_mode": "raw", "all_columns": columns,
            "adhoc_filters": list(filters), "row_limit": limit,
            "order_by_cols": [json.dumps([order[0], order[1]])] if order else [],
            "include_search": False, "server_pagination": False,
            "show_cell_bars": False, "table_timestamp_format": "%Y-%m-%d %H:%M",
            "column_config": {column: {"d3NumberFormat": fmt}
                              for column, fmt in (formats or {}).items()}}


VOL = where("target = 'volume'")
VTY = where("target = 'volatility'")

# (ключ, название, набор данных, вопрос пользователя, параметры, ширина, высота)
MARKET = [
    ("next_volume", "Прогноз объёма на завтра, BTC", "mart.v_next_day_forecast",
     "Сколько BTC наторгуют завтра?",
     number("MAX(y_pred)", "Прогноз объёма", [VOL], ",.0f",
            "модель-чемпион, 80-процентный интервал — в таблице ниже"), 3, 25),
    ("next_volume_dev", "Объём завтра относительно среднего за 30 дней, %",
     "mart.v_next_day_forecast", "Будет ли завтра необычная активность?",
     number("MAX(deviation_pct)", "Отклонение, %", [VOL], "+.1f",
            "плюс — выше обычного уровня, минус — ниже"), 3, 25),
    ("next_vol", "Прогноз волатильности на завтра", "mart.v_next_day_forecast",
     "Насколько сильно будет колебаться цена завтра?",
     number("MAX(y_pred)", "Прогноз волатильности", [VTY], ".4f",
            "оценка Паркинсона по максимуму и минимуму суток"), 3, 25),
    ("next_vol_dev", "Волатильность завтра относительно среднего за 30 дней, %",
     "mart.v_next_day_forecast", "Будут ли завтра необычные колебания цены?",
     number("MAX(deviation_pct)", "Отклонение, %", [VTY], "+.1f",
            "плюс — выше обычного уровня, минус — ниже"), 3, 25),
    ("next_table", "Прогноз на завтра с интервалом", "mart.v_next_day_forecast",
     "В каких пределах ожидать значения завтра?",
     table(["target_name", "target_date", "y_pred", "lower_80", "upper_80",
            "typical_30d", "deviation_pct", "model"],
           formats={"y_pred": ",.4~r", "lower_80": ",.4~r", "upper_80": ",.4~r",
                    "typical_30d": ",.4~r", "deviation_pct": "+.1f"}), 12, 25),
    ("fva_volume", "Объём: прогноз и факт на отложенной выборке",
     "mart.v_forecast_vs_actual", "Насколько прогнозу объёма можно верить?",
     line("target_date", [metric("AVG(y_true)", "Факт"), metric("AVG(y_pred)", "Прогноз"),
                          metric("AVG(lower_80)", "Нижняя граница 80 %"),
                          metric("AVG(upper_80)", "Верхняя граница 80 %")], [VOL]), 6, 50),
    ("fva_vol", "Волатильность: прогноз и факт на отложенной выборке",
     "mart.v_forecast_vs_actual", "Насколько прогнозу волатильности можно верить?",
     line("target_date", [metric("AVG(y_true)", "Факт"), metric("AVG(y_pred)", "Прогноз"),
                          metric("AVG(lower_80)", "Нижняя граница 80 %"),
                          metric("AVG(upper_80)", "Верхняя граница 80 %")], [VTY]), 6, 50),
    ("volume_history", "Объём торгов и среднее за 30 дней, BTC", "mart.v_market_daily",
     "Каков обычный уровень активности и как он менялся?",
     line("candle_date", [metric("AVG(volume_btc)", "Объём за сутки"),
                          metric("AVG(volume_ma30)", "Среднее за 30 дней")]), 6, 50),
    ("vol_history", "Волатильность и среднее за 30 дней", "mart.v_market_daily",
     "Каков обычный уровень колебаний цены?",
     line("candle_date", [metric("AVG(volatility_pk)", "Волатильность за сутки"),
                          metric("AVG(volatility_ma30)", "Среднее за 30 дней")]), 6, 50),
    ("by_weekday", "Средний объём по дням недели, BTC", "mart.v_market_daily",
     "Есть ли недельная цикличность активности?",
     bar("weekday", [metric("AVG(volume_btc)", "Средний объём")]), 4, 45),
    ("by_year", "Средний объём по годам, BTC", "mart.v_market_daily",
     "Как менялся уровень активности по годам?",
     bar("year", [metric("AVG(volume_btc)", "Средний объём")]), 4, 45),
    ("by_sentiment", "Средний объём по настроению рынка, BTC", "mart.v_market_daily",
     "Связаны ли настроения участников с активностью торгов?",
     bar("fear_greed_band", [metric("AVG(volume_btc)", "Средний объём")],
         [where("fear_greed_band IS NOT NULL")], xAxisLabelRotation=45), 4, 45),
    ("model_quality", "Качество моделей на отложенной выборке", "mart.v_model_quality",
     "Лучше ли модель простого правила «завтра как сегодня»?",
     table(["target_name", "model", "cv_mae", "test_mae", "mape_pct", "r2",
            "mae_vs_naive", "coverage_80", "is_champion"], order=("target_name", True),
           formats={"cv_mae": ",.4~r", "test_mae": ",.4~r", "mape_pct": ".1f",
                    "r2": ".3f", "mae_vs_naive": ".3f", "coverage_80": ".0%"}), 12, 50),
    ("external", "Проверка пользы внешних факторов (тест Диболда — Мариано)",
     "mart.v_external_factor_test",
     "Помогают ли курс ЦБ, индекс страха и ставка ФРС прогнозу?",
     table(["target_name", "model_name", "mae_without_external", "mae_with_external",
            "p_value", "verdict"],
           formats={"mae_without_external": ",.4~r", "mae_with_external": ",.4~r",
                    "p_value": ".4f"}), 12, 30),
    ("importance", "Важность признаков модели-чемпиона: объём", "mart.v_feature_importance",
     "От чего сильнее всего зависит прогноз объёма?",
     bar("feature", [metric("SUM(importance)", "Важность")],
         [VOL, where("importance > 0.001")], x_axis_sort="Важность",
         x_axis_sort_asc=False, orientation="horizontal"), 6, 50),
    ("importance_vol", "Важность признаков модели-чемпиона: волатильность",
     "mart.v_feature_importance", "От чего сильнее всего зависит прогноз волатильности?",
     bar("feature", [metric("SUM(importance)", "Важность")],
         [VTY, where("importance > 0.001")], x_axis_sort="Важность",
         x_axis_sort_asc=False, orientation="horizontal"), 6, 50),
]

OPS = [
    ("load_status", "Состояние источников", "meta.v_load_status",
     "Все ли источники загружаются вовремя?",
     table(["source", "last_status", "last_run", "last_success", "last_loaded_date",
            "days_behind", "error_message"]), 7, 30),
    ("runs", "Запуски конвейера", "meta.v_pipeline_runs",
     "Отрабатывает ли конвейер целиком и сколько времени занимает?",
     table(["run_id", "status", "started_at", "duration_sec", "error_message"],
           order=("started_at", False), limit=30), 5, 30),
    ("rows_added", "Добавлено строк по дням и источникам", "meta.v_load_history",
     "Сколько новых данных приходит от каждого источника?",
     bar("run_date", [metric("SUM(rows_added)", "Добавлено строк")], groupby=["source"],
         temporal=True, stack="Stack"), 6, 45),
    ("failures", "Сбои загрузки по дням", "meta.v_load_history",
     "Бывают ли сбои источников и какие?",
     bar("run_date", [metric("SUM(failures)", "Сбоев")], groupby=["source"],
         temporal=True, stack="Stack"), 6, 45),
    ("quality_latest", "Проверки качества последнего запуска", "meta.v_quality_latest",
     "Прошли ли данные проверки качества перед прогнозом?",
     table(["check_type", "table_name", "check_name", "passed", "severity", "action",
            "details"], order=("passed", True)), 12, 55),
    ("quality_history", "Непройденные проверки по дням и критичности",
     "meta.v_quality_history", "Как часто обнаруживаются проблемы качества?",
     bar("check_date", [metric("SUM(failed)", "Непройдено")], groupby=["severity"],
         temporal=True, stack="Stack"), 6, 45),
    ("revisions", "Пересмотры значений внешних факторов", "meta.v_factor_revisions",
     "Меняли ли источники уже загруженные значения задним числом?",
     table(["factor_code", "factor_date", "version_no", "value", "valid_from",
            "valid_to", "is_current"]), 6, 45),
    ("lineage", "Происхождение показателей: от источника до дашборда", "meta.v_lineage",
     "Откуда берётся каждое число на предметном дашборде?",
     table(["from_dataset", "from_column", "to_dataset", "to_column", "transformation",
            "process"], order=("edge_id", True)), 12, 60),
]

TIME_COLUMNS = {
    "mart.v_market_daily": "candle_date", "mart.v_forecast_vs_actual": "target_date",
    "mart.v_next_day_forecast": "target_date", "meta.v_load_history": "run_date",
    "meta.v_quality_history": "check_date", "meta.v_pipeline_runs": "started_at",
}


def query_context(dataset_id, params):
    # Минимальный запрос для проверки графика через API: выполняется тот же
    # SQL по тем же столбцам, что при отображении на дашборде
    metrics = params.get("metrics") or ([params["metric"]] if "metric" in params else [])
    columns = params.get("all_columns") or (
        [params["x_axis"], *params.get("groupby", [])] if "x_axis" in params else [])
    wheres = [f["sqlExpression"] for f in params.get("adhoc_filters", [])
              if f.get("expressionType") == "SQL"]
    query = {"columns": columns, "metrics": metrics, "filters": [],
             "extras": {"where": " AND ".join(f"({w})" for w in wheres)},
             "row_limit": params.get("row_limit", 1000), "orderby": []}
    if params.get("query_mode") == "raw":
        query["metrics"] = None
    return {"datasource": {"id": dataset_id, "type": "table"}, "force": True,
            "queries": [query], "result_format": "json", "result_type": "full",
            "form_data": params}


def build_dashboard(api, slug, title, intro, specs, datasets, filters):
    for board in api.list("dashboard"):
        if board["slug"] == slug:
            api.call("DELETE", f"/api/v1/dashboard/{board['id']}")
    names = {spec[1] for spec in specs}
    for chart in api.list("chart"):
        if chart["slice_name"] in names:
            api.call("DELETE", f"/api/v1/chart/{chart['id']}")

    board_id = api.call("POST", "/api/v1/dashboard/", json={
        "dashboard_title": title, "slug": slug, "published": True})["id"]

    charts = {}
    for key, name, dataset, question, params, width, height in specs:
        dataset_id = datasets[dataset]
        params = {**params, "datasource": f"{dataset_id}__table"}
        chart_id = api.call("POST", "/api/v1/chart/", json={
            "slice_name": name, "viz_type": params["viz_type"],
            "datasource_id": dataset_id, "datasource_type": "table",
            "description": question, "params": json.dumps(params, ensure_ascii=False),
            "query_context": json.dumps(query_context(dataset_id, params),
                                        ensure_ascii=False),
            "dashboards": [board_id]})["id"]
        charts[key] = (chart_id, name, dataset, width, height)

    # Раскладка: строки заполняются слева направо до ширины 12
    layout = {"DASHBOARD_VERSION_KEY": "v2",
              "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
              "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [],
                          "parents": ["ROOT_ID"]},
              "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}}}
    rows = [["MARKDOWN-intro"]]
    layout["MARKDOWN-intro"] = {"type": "MARKDOWN", "id": "MARKDOWN-intro", "children": [],
                                "parents": ["ROOT_ID", "GRID_ID", "ROW-0"],
                                "meta": {"width": 12, "height": 22, "code": intro}}
    current, used = [], 0
    for key, (chart_id, name, _, width, height) in charts.items():
        if used + width > 12:
            rows.append(current)
            current, used = [], 0
        current.append(f"CHART-{key}")
        used += width
        layout[f"CHART-{key}"] = {"type": "CHART", "id": f"CHART-{key}", "children": [],
                                  "meta": {"width": width, "height": height,
                                           "chartId": chart_id, "sliceName": name}}
    rows.append(current)
    for index, children in enumerate(rows):
        row_id = f"ROW-{index}"
        layout["GRID_ID"]["children"].append(row_id)
        layout[row_id] = {"type": "ROW", "id": row_id, "children": children,
                          "parents": ["ROOT_ID", "GRID_ID"],
                          "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for child in children:
            layout[child]["parents"] = ["ROOT_ID", "GRID_ID", row_id]

    native = []
    for filter_id, name, kind, dataset, column, description in filters:
        scoped = [c[0] for c in charts.values()
                  if kind == "filter_time" and c[2] in TIME_COLUMNS
                  or kind == "filter_select" and c[2] == dataset]
        excluded = [c[0] for c in charts.values() if c[0] not in scoped]
        target = ({} if kind == "filter_time"
                  else {"datasetId": datasets[dataset], "column": {"name": column}})
        native.append({
            "id": filter_id, "name": name, "filterType": kind, "type": "NATIVE_FILTER",
            "targets": [target], "description": description,
            "controlValues": ({} if kind == "filter_time" else
                              {"multiSelect": True, "enableEmptyFilter": False,
                               "defaultToFirstItem": False, "searchAllOptions": False,
                               "inverseSelection": False}),
            "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
            "cascadeParentIds": [], "tabsInScope": [], "chartsInScope": scoped,
            "scope": {"rootPath": ["ROOT_ID"], "excluded": excluded}})

    metadata = {"native_filter_configuration": native, "refresh_frequency": 300,
                "color_scheme": "supersetColors", "cross_filters_enabled": True,
                "expanded_slices": {}, "label_colors": {}, "shared_label_colors": {},
                "timed_refresh_immune_slices": [], "default_filters": "{}",
                "chart_configuration": {}}
    api.call("PUT", f"/api/v1/dashboard/{board_id}", json={
        "position_json": json.dumps(layout, ensure_ascii=False),
        "json_metadata": json.dumps(metadata, ensure_ascii=False)})
    return board_id, charts


def check_charts(api, charts):
    problems = []
    for chart_id, name, *_ in charts.values():
        try:
            chart = api.call("GET", f"/api/v1/chart/{chart_id}")["result"]
            context = json.loads(chart["query_context"])
            result = api.call("POST", "/api/v1/chart/data", json=context)["result"][0]
            if result.get("error"):
                problems.append(f"{name}: {result['error']}")
            elif not result.get("rowcount"):
                print(f"  пусто: {name}")
        except RuntimeError as exc:
            problems.append(f"{name}: {exc}")
    return problems


MARKET_INTRO = """### Для кого и зачем
Аналитик рынка цифровых активов видит **прогноз объёма торгов и волатильности BTC/USDT на OKX на завтра**
и сравнивает его с обычным уровнем за 30 дней. Верхний ряд отвечает на вопрос «будет ли завтра необычная
активность», ниже — насколько прогнозу можно верить и какие закономерности есть в истории.
Прогноз не является торговой рекомендацией: направление цены система не предсказывает."""

OPS_INTRO = """### Состояние конвейера данных
Для сопровождающего систему: **свежесть источников, сбои загрузки, результаты проверок качества,
пересмотры значений и происхождение показателей**. Если проверка с критичностью *critical* не пройдена,
конвейер останавливается до построения прогноза — причина видна в таблице проверок."""


def main():
    views = {spec[2] for spec in MARKET + OPS}
    wait_for_views(views)
    api = Api()
    database_id = ensure_database(api)
    datasets = {}
    for view in sorted(views):
        schema, table_name = view.split(".")
        datasets[view] = ensure_dataset(api, database_id, schema, table_name,
                                        TIME_COLUMNS.get(view))
    print(f"наборов данных: {len(datasets)}")

    market_id, market_charts = build_dashboard(
        api, MARKET_SLUG, "Рынок BTC/USDT: прогноз активности и волатильности",
        MARKET_INTRO, MARKET, datasets,
        [("NATIVE_FILTER-period", "Период", "filter_time", None, None,
          "Ограничивает графики по датам"),
         ("NATIVE_FILTER-year", "Год", "filter_select", "mart.v_market_daily", "year",
          "Разрез истории рынка по годам"),
         ("NATIVE_FILTER-weekday", "День недели", "filter_select", "mart.v_market_daily",
          "weekday", "Разрез истории рынка по дням недели")])
    ops_id, ops_charts = build_dashboard(
        api, OPS_SLUG, "Конвейер данных: загрузка, качество, происхождение",
        OPS_INTRO, OPS, datasets,
        [("NATIVE_FILTER-period", "Период", "filter_time", None, None,
          "Ограничивает графики по датам"),
         ("NATIVE_FILTER-source", "Источник", "filter_select", "meta.v_load_history",
          "source", "Разрез загрузок по источнику"),
         ("NATIVE_FILTER-severity", "Критичность", "filter_select",
          "meta.v_quality_history", "severity", "Разрез проверок по критичности")])

    problems = check_charts(api, {**market_charts, **ops_charts})
    print(f"предметный дашборд: {BASE}/superset/dashboard/{MARKET_SLUG}/ "
          f"({len(market_charts)} графиков)")
    print(f"операционный дашборд: {BASE}/superset/dashboard/{OPS_SLUG}/ "
          f"({len(ops_charts)} графиков)")
    if problems:
        print("ошибки графиков:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("все графики выполняются без ошибок")


if __name__ == "__main__":
    main()
