# Запуск всего конвейера одной командой
#
#   загрузка 4 источников → clean → проверки качества → витрина →
#   проверки витрины → модели и прогноз → метаданные и представления
#
# Если проверка с критичностью critical не пройдена, конвейер
# останавливается до построения прогноза: пользователь увидит на
# операционном дашборде причину, а не прогноз по испорченным данным.
#
# Однократно:      python -m okx_btc_pas.pipeline
# По расписанию:   python -m okx_btc_pas.pipeline --daily-at 00:30
#   (время UTC; OKX закрывает сутки в 00:00 UTC, запас на публикацию)

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine

from .cleaning import build_clean
from .db import initialize_database
from .ingestion import SOURCES, run_loader
from .lineage import record_run, register_metadata
from .mart import build_mart
from .model import run_models
from .quality import run_mart_checks, run_source_checks
from .views import create_views, grant_bi_reader


def run_pipeline(engine, now=None, session=None):
    started = now or datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    steps = {}
    status, error = "success", None
    try:
        initialize_database(engine)
        register_metadata(engine)

        loads = {}
        for source in SOURCES:
            try:
                result = run_loader(engine, source, session=session, run_id=run_id)
                loads[source] = f"+{result['rows_added']} ~{result['rows_updated']}"
            except RuntimeError as exc:
                # Сбой одного источника не прерывает запуск: решение о
                # допустимости принимают проверки свежести ниже
                loads[source] = f"сбой: {exc.args[0].get('error_message')}"
        steps["load"] = loads

        steps["clean"] = build_clean(engine, run_id=run_id)
        checks = run_source_checks(engine, run_id=run_id)
        steps["quality_sources"] = checks
        if checks["critical"]:
            status = "stopped_by_quality"
            return _finish(engine, run_id, started, status, steps, None)

        steps["mart"] = build_mart(engine)
        checks = run_mart_checks(engine, run_id=run_id)
        steps["quality_mart"] = checks
        if checks["critical"]:
            status = "stopped_by_quality"
            return _finish(engine, run_id, started, status, steps, None)

        summary = run_models(engine, run_id=run_id)
        steps["models"] = {target: {"champion": summary[target]["champion"],
                                    "next_day": summary[target].get("next_day")}
                           for target in ("volume", "volatility")}
        steps["views"] = create_views(engine)
        grant_bi_reader(engine, os.environ.get("BI_READER_PASSWORD"))
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"[:2000]
    return _finish(engine, run_id, started, status, steps, error)


def _finish(engine, run_id, started, status, steps, error):
    record_run(engine, run_id, started, status, steps, error)
    return {"run_id": run_id, "status": status, "steps": steps, "error": error}


def _seconds_until(hour, minute):
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main():
    parser = argparse.ArgumentParser(description="Запуск конвейера ПАС")
    parser.add_argument("--daily-at", metavar="HH:MM",
                        help="запускать ежедневно в указанное время UTC")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url)

    if not args.daily_at:
        result = run_pipeline(engine)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(0 if result["status"] == "success" else 1)

    hour, minute = (int(part) for part in args.daily_at.split(":"))
    # Первый запуск сразу, чтобы после развёртывания данные появились без ожидания
    while True:
        result = run_pipeline(engine)
        print(json.dumps({k: result[k] for k in ("run_id", "status", "error")},
                         ensure_ascii=False), flush=True)
        wait = _seconds_until(hour, minute)
        print(f"следующий запуск через {wait / 3600:.1f} ч", flush=True)
        time.sleep(wait)


if __name__ == "__main__":
    main()
