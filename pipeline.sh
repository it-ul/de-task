#!/bin/bash
set -e
export PGPASSWORD="${PGPASSWORD:-postgres}"
PG="psql -h ${DWH_HOST:-pg17} -U postgres -v ON_ERROR_STOP=1"
PGD="psql -h ${DWH_HOST:-pg17} -U postgres -v ON_ERROR_STOP=0"

echo "Ожидание БД..."
until $PG -d postgres -c '\q' >/dev/null 2>&1; do
  sleep 2
done
echo "БД доступна"

echo "Создаю базы, если отсутствуют..."
$PG -d postgres <<'EOSQL'
SELECT 'CREATE DATABASE deals_bigdata'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'deals_bigdata')\gexec
SELECT 'CREATE DATABASE dwh'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dwh')\gexec
EOSQL

MARKER=$($PG -d dwh -tAc "SELECT to_regclass('public.init_done') IS NOT NULL")
if [ "$MARKER" != "t" ]; then
  echo "Первый запуск: применяю schema.sql к deals_bigdata..."
  $PG -d deals_bigdata -f /sql/schema.sql
  echo "Генерирую синтетические данные (n_prices=${N_PRICES:-300000})..."
  $PG -d deals_bigdata -v n_prices="${N_PRICES:-300000}" -f /sql/seed/synthetic.sql
  $PG -d dwh -c "CREATE TABLE public.init_done (id int PRIMARY KEY DEFAULT 1)"
  echo "Инициализация завершена"
else
  echo "Инициализация уже выполнена — пропускаю схему и синтетику"
fi

echo "Схемы, если отсутствуют..."
$PG -d dwh -c "CREATE SCHEMA IF NOT EXISTS raw;
               CREATE SCHEMA IF NOT EXISTS core;
               CREATE SCHEMA IF NOT EXISTS mart;"

echo "DDL до ETL..."
$PGD -d dwh -f /sql/migrations/000_raw_ddl.sql      # raw.load_runs и таблицы raw (нужно run_etl)
$PGD -d dwh -f /sql/migrations/001_core_ddl.sql     # таблицы core (нужно run_core)
$PGD -d dwh -f /sql/migrations/003_price_types.sql  # справочник ролей (статичный, без данных)
$PGD -d dwh -f /sql/migrations/001_mart_ddl.sql     # DDL витрин mart

echo "ETL: загрузка raw..."
if [ -z "${ETL_START:-}" ] || [ -z "${ETL_END:-}" ]; then
  ETL_START=$($PG -d deals_bigdata -tAc "SELECT min(loading_date)::text FROM prices")
  ETL_END=$($PG -d deals_bigdata -tAc "SELECT max(loading_date)::text FROM prices")
  echo "Диапазон выгрузки из данных: ${ETL_START} … ${ETL_END}"
fi
python -m etl.run_etl --start "$ETL_START" --end "$ETL_END"
echo "ETL: загрузка core..."
python -m etl.run_core --start "$ETL_START" --end "$ETL_END"

echo "Миграции после ETL..."
$PG -d dwh -f /sql/migrations/002_ml_ddl.sql
$PG -d dwh -f /sql/migrations/005_core_price_dates.sql
$PG -d dwh -f /sql/migrations/004_mart_corridor_ddl.sql   # DDL коридора
$PG -d dwh -f /sql/migrations/004_mart_corridor.sql       # заполнение коридора
$PG -d dwh -f /sql/migrations/006_ml_predictions.sql

echo "Витрины (run_mart)..."
python -m etl.run_mart

echo "ML..."
python -m ml.run_ml

echo "PIPELINE_DONE"
