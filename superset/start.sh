#!/bin/sh
# Инициализация метаданных Superset и запуск веб-сервера
set -e
superset db upgrade
superset fab create-admin --username "$SUPERSET_ADMIN_USER" --firstname Admin \
  --lastname PAS --email admin@example.com --password "$SUPERSET_ADMIN_PASSWORD" || true
superset init
exec /usr/bin/run-server.sh
