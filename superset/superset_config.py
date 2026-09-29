# Настройки Superset для проекта. Секреты берутся из переменных окружения
import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
# Метаданные самого Superset (пользователи, графики, дашборды) хранятся
# в SQLite внутри тома superset_home; данные проекта — в PostgreSQL
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"

BABEL_DEFAULT_LOCALE = "ru"
LANGUAGES = {"ru": {"flag": "ru", "name": "Русский"},
             "en": {"flag": "us", "name": "English"}}

FEATURE_FLAGS = {"DASHBOARD_NATIVE_FILTERS": True, "DASHBOARD_CROSS_FILTERS": True}
# Кэш результатов на 5 минут: дашборд не нагружает базу при каждом открытии,
# а свежие данные после запуска конвейера появляются не позже чем через 5 минут
DATA_CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
