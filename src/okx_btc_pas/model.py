# Прогнозное ядро: объём торгов и волатильность на следующие сутки
#
# Схема проверки:
#   обучение  — сутки до TEST_START, внутри них скользящая проверка по времени
#   отложенная проверка — сутки начиная с TEST_START, в подборе не участвуют
# Модель-чемпион выбирается по ошибке на скользящей проверке внутри обучения,
# а отложенная выборка только измеряет итог: иначе выбор подгонялся бы под неё
#
# Модели: две базовые (завтра как сегодня, среднее за 7 дней), гребневая
# регрессия и градиентный бустинг. Каждая обучается на двух наборах
# признаков — без внешних факторов и с ними, чтобы проверить их пользу

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import Ridge
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, String, Table,
    create_engine, select,
)

from .db import initialize_database, metadata
from .mart import daily_mart

TEST_START = date(2025, 1, 1)
CV_SPLITS = 5
INTERVAL = (0.10, 0.90)   # 80-процентный интервал прогноза

TARGETS = {
    # цель: (столбец цели, столбец значения за сегодня, сдвиг для логарифма)
    "volume": ("target_volume_btc", "volume_btc", 1.0),
    "volatility": ("target_volatility_pk", "volatility_pk", 1e-4),
}

VOLUME_COLUMNS = ["volume_btc", "volume_lag_1", "volume_lag_2", "volume_lag_3",
                  "volume_lag_7", "volume_mean_7", "volume_mean_30"]
VOLATILITY_COLUMNS = ["volatility_pk", "volatility_lag_1", "volatility_lag_2",
                      "volatility_lag_3", "volatility_lag_7", "volatility_mean_7",
                      "volatility_mean_30"]
# День недели прогнозируемых суток кодируется отдельным признаком на
# каждый день: одним числом 0–6 линейная модель могла бы выучить только
# монотонную зависимость, а реальный эффект — провал именно в выходные.
# Месяц кодируется синусом и косинусом, чтобы декабрь был рядом с январём
CALENDAR_COLUMNS = [f"next_dow_{d}" for d in range(7)] + ["month_sin", "month_cos"]
MARKET_FEATURES = VOLUME_COLUMNS + VOLATILITY_COLUMNS + ["log_return"] + CALENDAR_COLUMNS
EXTERNAL_FEATURES = ["usd_rub", "usd_rub_age_days", "fear_greed",
                     "fear_greed_age_days", "fed_rate", "fed_rate_age_days"]
FEATURE_SETS = {
    "market": MARKET_FEATURES,
    "market_external": MARKET_FEATURES + EXTERNAL_FEATURES,
}

model_run = Table(
    "model_run", metadata,
    Column("run_id", String(40), primary_key=True),
    Column("target", String(20), primary_key=True),
    Column("model_name", String(30), primary_key=True),
    Column("feature_set", String(30), primary_key=True),
    Column("trained_at", DateTime(timezone=True), nullable=False),
    Column("train_start", Date), Column("train_end", Date),
    Column("test_start", Date), Column("test_end", Date),
    Column("n_train", Integer), Column("n_test", Integer),
    Column("cv_mae_mean", Float), Column("cv_mae_std", Float),
    Column("mae", Float), Column("rmse", Float), Column("mape", Float),
    Column("r2", Float),
    # Ошибка модели относительно ошибки прогноза «завтра как сегодня»:
    # меньше 1 — модель полезнее простейшего правила
    Column("mae_vs_naive", Float),
    # Доля дней отложенной выборки, где факт попал в 80-процентный интервал
    Column("coverage_80", Float),
    Column("is_champion", Boolean, nullable=False),
    schema="mart",
)
forecast = Table(
    "forecast", metadata,
    Column("run_id", String(40), primary_key=True),
    Column("target", String(20), primary_key=True),
    Column("model_name", String(30), primary_key=True),
    Column("feature_set", String(30), primary_key=True),
    Column("target_date", Date, primary_key=True),
    Column("feature_date", Date, nullable=False),
    Column("y_pred", Float, nullable=False),
    Column("lower_80", Float), Column("upper_80", Float),
    Column("y_true", Float),
    # true — прогноз на прошедший день отложенной выборки,
    # false — прогноз на завтра, факт по которому ещё не известен
    Column("is_backtest", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="mart",
)
model_comparison = Table(
    "model_comparison", metadata,
    Column("run_id", String(40), primary_key=True),
    Column("target", String(20), primary_key=True),
    Column("model_name", String(30), primary_key=True),
    Column("mae_market", Float), Column("mae_external", Float),
    Column("dm_statistic", Float), Column("p_value", Float),
    Column("verdict", String(200)),
    schema="mart",
)
feature_importance = Table(
    "feature_importance", metadata,
    Column("run_id", String(40), primary_key=True),
    Column("target", String(20), primary_key=True),
    Column("model_name", String(30), primary_key=True),
    Column("feature_set", String(30), primary_key=True),
    Column("feature", String(40), primary_key=True),
    Column("importance", Float, nullable=False),
    schema="mart",
)


# Читаем витрину и добавляем календарь дня прогноза
# День недели завтрашних суток известен заранее, поэтому утечкой не является
def load_frame(conn):
    frame = pd.read_sql(select(daily_mart), conn)
    # Имена столбцов приходят объектами SQLAlchemy, scikit-learn ждёт строки
    frame.columns = [str(column) for column in frame.columns]
    frame["candle_date"] = pd.to_datetime(frame["candle_date"]).dt.date
    numeric = [c for c in frame.columns if c not in ("candle_date", "built_at")]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    next_dow = (frame["day_of_week"] + 1) % 7
    for day in range(7):
        frame[f"next_dow_{day}"] = (next_dow == day).astype(int)
    frame["month_sin"] = np.sin(2 * np.pi * frame["month"] / 12)
    frame["month_cos"] = np.cos(2 * np.pi * frame["month"] / 12)
    return frame.sort_values("candle_date").reset_index(drop=True)


# Объёмные признаки распределены с тяжёлым хвостом, поэтому логарифмируются
def prepare_features(frame, features):
    data = frame[features].astype(float).copy()
    for column in features:
        if column in VOLUME_COLUMNS:
            data[column] = np.log1p(data[column])
        elif column in VOLATILITY_COLUMNS:
            data[column] = np.log(data[column] + 1e-4)
    return data


def check_no_leakage(features):
    leaked = [f for f in features if f.startswith("target_")]
    if leaked:
        raise ValueError(f"Целевые значения попали в признаки: {leaked}")


def _to_log(values, shift):
    return np.log(np.asarray(values, dtype=float) + shift)


def _from_log(values, shift):
    return np.exp(np.asarray(values, dtype=float)) - shift


def make_model(name):
    if name == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    if name == "gbm":
        return HistGradientBoostingRegressor(
            max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
            min_samples_leaf=30, l2_regularization=1.0, random_state=0)
    raise ValueError(name)


# Базовые правила не обучаются: прогноз вычисляется из значений за сегодня
def baseline_predict(name, frame, today_column):
    today = frame[today_column].astype(float).to_numpy()
    if name == "naive":
        return today
    if name == "ma7":
        # Среднее за 7 суток, включая сегодняшние: mean(d-6..d)
        mean_prev = frame[f"{today_column.split('_')[0]}_mean_7"].astype(float).to_numpy()
        lag7 = frame[f"{today_column.split('_')[0]}_lag_7"].astype(float).to_numpy()
        return (7 * mean_prev - lag7 + today) / 7
    raise ValueError(name)


def metrics(y_true, y_pred, naive_mae=None):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    error = y_pred - y_true
    mae = float(np.mean(np.abs(error)))
    ss_res = float(np.sum(error ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    return {
        "mae": mae,
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "mape": float(np.mean(np.abs(error) / np.abs(y_true)) * 100),
        "r2": 1 - ss_res / ss_tot if ss_tot else None,
        "mae_vs_naive": mae / naive_mae if naive_mae else None,
    }


# Одна модель на одном наборе признаков: скользящая проверка внутри обучения,
# интервал по остаткам последнего окна проверки, итог на отложенной выборке
def evaluate(model_name, target, feature_set, train, test):
    target_column, today_column, shift = TARGETS[target]
    features = FEATURE_SETS[feature_set]
    check_no_leakage(features)
    y_train_log = _to_log(train[target_column], shift)

    folds = list(TimeSeriesSplit(n_splits=CV_SPLITS).split(train))
    cv_errors = []
    last_residuals = None
    for fit_index, check_index in folds:
        fit, check = train.iloc[fit_index], train.iloc[check_index]
        if model_name in ("naive", "ma7"):
            pred = baseline_predict(model_name, check, today_column)
            pred_log = _to_log(np.clip(pred, 0, None), shift)
        else:
            model = make_model(model_name)
            model.fit(prepare_features(fit, features), y_train_log[fit_index])
            pred_log = model.predict(prepare_features(check, features))
            pred = _from_log(pred_log, shift)
        cv_errors.append(float(np.mean(np.abs(pred - check[target_column]))))
        last_residuals = y_train_log[check_index] - pred_log

    low_q, high_q = np.quantile(last_residuals, INTERVAL)

    if model_name in ("naive", "ma7"):
        fitted = None
        test_pred = baseline_predict(model_name, test, today_column)
        test_pred_log = _to_log(np.clip(test_pred, 0, None), shift)
    else:
        fitted = make_model(model_name)
        fitted.fit(prepare_features(train, features), y_train_log)
        test_pred_log = fitted.predict(prepare_features(test, features))
        test_pred = _from_log(test_pred_log, shift)

    lower = _from_log(test_pred_log + low_q, shift)
    upper = _from_log(test_pred_log + high_q, shift)
    y_test = test[target_column].to_numpy(dtype=float)
    coverage = float(np.mean((y_test >= lower) & (y_test <= upper)))
    return {
        "cv_mae_mean": float(np.mean(cv_errors)),
        "cv_mae_std": float(np.std(cv_errors)),
        "test_pred": test_pred, "lower": lower, "upper": upper,
        "coverage_80": coverage, "interval": (float(low_q), float(high_q)),
        "fitted": fitted,
    }


# Важность признаков: насколько растёт ошибка на отложенной выборке,
# если перемешать значения одного признака
def importance(fitted, target, feature_set, test):
    target_column, _, shift = TARGETS[target]
    features = FEATURE_SETS[feature_set]
    result = permutation_importance(
        fitted, prepare_features(test, features),
        _to_log(test[target_column], shift),
        scoring="neg_mean_absolute_error", n_repeats=5, random_state=0)
    return dict(zip(features, result.importances_mean))


# Тест Диболда — Мариано: случайна ли разница ошибок двух прогнозов
# на одних и тех же днях. Дисперсия считается с поправкой Ньюи — Уэста,
# так как ошибки соседних дней связаны между собой
def diebold_mariano(errors_a, errors_b, max_lag=7):
    diff = np.abs(np.asarray(errors_a)) - np.abs(np.asarray(errors_b))
    n = len(diff)
    centered = diff - diff.mean()
    variance = np.sum(centered ** 2) / n
    for lag in range(1, max_lag + 1):
        weight = 1 - lag / (max_lag + 1)
        variance += 2 * weight * np.sum(centered[lag:] * centered[:-lag]) / n
    if variance <= 0:
        return 0.0, 1.0
    statistic = float(diff.mean() / np.sqrt(variance / n))
    p_value = float(math.erfc(abs(statistic) / math.sqrt(2)))
    return statistic, p_value


def verdict_for(p_value, mae_market, mae_external):
    if p_value >= 0.05:
        return "разница незначима (p ≥ 0,05): польза внешних факторов не подтверждена"
    if mae_external < mae_market:
        return "внешние факторы значимо снижают ошибку (p < 0,05)"
    return "внешние факторы значимо увеличивают ошибку (p < 0,05)"


MODELS = ("naive", "ma7", "ridge", "gbm")


def run_models(engine, run_id=None, now=None, test_start=TEST_START):
    moment = now or datetime.now(timezone.utc)
    run_id = run_id or moment.strftime("%Y%m%dT%H%M%SZ")
    initialize_database(engine)

    with engine.connect() as conn:
        frame = load_frame(conn)

    all_features = sorted(set(MARKET_FEATURES + EXTERNAL_FEATURES))
    # Одинаковый набор строк для всех моделей, иначе сравнение нечестное
    usable = frame.dropna(subset=all_features).reset_index(drop=True)
    labeled = usable.dropna(subset=[c for c, _, _ in TARGETS.values()])
    train = labeled[labeled["candle_date"] < test_start].reset_index(drop=True)
    test = labeled[labeled["candle_date"] >= test_start].reset_index(drop=True)
    if len(train) < 100 or test.empty:
        raise ValueError("Недостаточно данных для обучения и проверки")
    # Порядок по времени: ни одна строка проверки не раньше обучения
    assert train["candle_date"].max() < test["candle_date"].min()

    runs, forecasts, importances, comparisons, summary = [], [], [], [], {}
    for target, (target_column, today_column, shift) in TARGETS.items():
        naive_test_mae = float(np.mean(np.abs(
            baseline_predict("naive", test, today_column) - test[target_column])))
        results = {}
        for model_name in MODELS:
            sets = ("market",) if model_name in ("naive", "ma7") else tuple(FEATURE_SETS)
            for feature_set in sets:
                result = evaluate(model_name, target, feature_set, train, test)
                result["metrics"] = metrics(test[target_column], result["test_pred"],
                                            naive_test_mae)
                results[(model_name, feature_set)] = result

        # Чемпион — наименьшая ошибка скользящей проверки внутри обучения
        champion = min(results, key=lambda key: results[key]["cv_mae_mean"])
        summary[target] = {"champion": "/".join(champion), "models": {},
                           "external_factors": {}}

        y_test = test[target_column].to_numpy(dtype=float)
        for model_name in ("ridge", "gbm"):
            base = results[(model_name, "market")]
            ext = results[(model_name, "market_external")]
            statistic, p_value = diebold_mariano(base["test_pred"] - y_test,
                                                 ext["test_pred"] - y_test)
            verdict = verdict_for(p_value, base["metrics"]["mae"], ext["metrics"]["mae"])
            comparisons.append({
                "run_id": run_id, "target": target, "model_name": model_name,
                "mae_market": base["metrics"]["mae"],
                "mae_external": ext["metrics"]["mae"],
                "dm_statistic": statistic, "p_value": p_value, "verdict": verdict})
            summary[target]["external_factors"][model_name] = {
                "p_value": round(p_value, 4), "verdict": verdict}

        for (model_name, feature_set), result in results.items():
            m = result["metrics"]
            summary[target]["models"][f"{model_name}/{feature_set}"] = {
                "cv_mae": round(result["cv_mae_mean"], 6), "test_mae": round(m["mae"], 6),
                "mae_vs_naive": round(m["mae_vs_naive"], 4),
                "coverage_80": round(result["coverage_80"], 3)}
            runs.append({
                "run_id": run_id, "target": target, "model_name": model_name,
                "feature_set": feature_set, "trained_at": moment,
                "train_start": train["candle_date"].min(),
                "train_end": train["candle_date"].max(),
                "test_start": test["candle_date"].min(),
                "test_end": test["candle_date"].max(),
                "n_train": len(train), "n_test": len(test),
                "cv_mae_mean": result["cv_mae_mean"], "cv_mae_std": result["cv_mae_std"],
                **{k: m[k] for k in ("mae", "rmse", "mape", "r2", "mae_vs_naive")},
                "coverage_80": result["coverage_80"],
                "is_champion": (model_name, feature_set) == champion,
            })
            for i, row in test.iterrows():
                forecasts.append({
                    "run_id": run_id, "target": target, "model_name": model_name,
                    "feature_set": feature_set,
                    "target_date": row["candle_date"] + timedelta(days=1),
                    "feature_date": row["candle_date"],
                    "y_pred": float(result["test_pred"][i]),
                    "lower_80": float(result["lower"][i]),
                    "upper_80": float(result["upper"][i]),
                    "y_true": float(row[target_column]), "is_backtest": True,
                    "created_at": moment,
                })
            if result["fitted"] is not None:
                for feature, value in importance(result["fitted"], target,
                                                 feature_set, test).items():
                    importances.append({
                        "run_id": run_id, "target": target, "model_name": model_name,
                        "feature_set": feature_set, "feature": feature,
                        "importance": float(value)})

        # Прогноз на завтра: чемпион переобучается на всех размеченных сутках
        # и применяется к последним закрытым суткам, для которых цели ещё нет
        latest = usable[usable[target_column].isna()].tail(1)
        if not latest.empty:
            model_name, feature_set = champion
            low_q, high_q = results[champion]["interval"]
            if model_name in ("naive", "ma7"):
                value = baseline_predict(model_name, latest, today_column)
                value_log = _to_log(np.clip(value, 0, None), shift)
            else:
                final = make_model(model_name)
                final.fit(prepare_features(labeled, FEATURE_SETS[feature_set]),
                          _to_log(labeled[target_column], shift))
                value_log = final.predict(
                    prepare_features(latest, FEATURE_SETS[feature_set]))
                value = _from_log(value_log, shift)
            feature_date = latest["candle_date"].iloc[0]
            forecasts.append({
                "run_id": run_id, "target": target, "model_name": model_name,
                "feature_set": feature_set,
                "target_date": feature_date + timedelta(days=1),
                "feature_date": feature_date, "y_pred": float(value[0]),
                "lower_80": float(_from_log(value_log + low_q, shift)[0]),
                "upper_80": float(_from_log(value_log + high_q, shift)[0]),
                "y_true": None, "is_backtest": False, "created_at": moment,
            })
            summary[target]["next_day"] = {
                "date": str(feature_date + timedelta(days=1)),
                "forecast": round(float(value[0]), 6),
                "interval_80": [round(float(_from_log(value_log + low_q, shift)[0]), 6),
                                round(float(_from_log(value_log + high_q, shift)[0]), 6)]}

    # Прогнозы прошлых запусков не удаляются: это история прогнозов
    with engine.begin() as conn:
        conn.execute(model_run.insert(), runs)
        conn.execute(forecast.insert(), forecasts)
        if importances:
            conn.execute(feature_importance.insert(), importances)
        conn.execute(model_comparison.insert(), comparisons)

    summary["run_id"] = run_id
    summary["train"] = f"{train['candle_date'].min()} .. {train['candle_date'].max()} ({len(train)})"
    summary["test"] = f"{test['candle_date'].min()} .. {test['candle_date'].max()} ({len(test)})"
    return summary


def main():
    parser = argparse.ArgumentParser(description="Обучение и проверка моделей прогноза")
    parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)
    print(json.dumps(run_models(engine), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
