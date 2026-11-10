"""Конфигурация ETL: DSN-ы и параметры батчей читаем из окружения (.env).

Схема: явный DSN из окружения побеждает; иначе DSN собирается из частей.
Вне Docker дефолт DWH_HOST/SOURCE_HOST = pg17 (как в исходном проекте);
в docker-compose оба хоста переопределяются на имя сервиса pg17.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv опционален
    pass


def _dsn(explicit_env: str, host_env: str, default_db: str) -> str:
    explicit = os.getenv(explicit_env)
    if explicit:
        return explicit
    host = os.getenv(host_env, "pg17")
    port = os.getenv("DWH_PORT", "5432")
    user = os.getenv("DWH_USER", "postgres")
    password = os.getenv("DWH_PASSWORD", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{default_db}"


# Первоисточник (read-only) и хранилище (raw/core/mart)
SOURCE_DSN = _dsn("SOURCE_DSN", "SOURCE_HOST", "deals_bigdata")
DWH_DSN = _dsn("DWH_DSN", "DWH_HOST", "dwh")

# Размер батча строк при чтении/COPY
BATCH_SIZE = int(os.getenv("ETL_BATCH_SIZE", "20000"))
# Размер чанка price_id для дочерних таблиц
ID_CHUNK = int(os.getenv("ETL_ID_CHUNK", "50000"))
