"""Загрузка слоя mart: полная пересборка витрин за прогон (идемпотентно)."""
import logging
import uuid

import psycopg
from psycopg.rows import dict_row

from .config import DWH_DSN

log = logging.getLogger("etl.mart")

# as_of — дата снимка данных по ТЗ. Константа живёт здесь локально, чтобы слой
# mart не тянул train.py (и его ML-зависимости) ради одной даты.
AS_OF_DATE = "2026-10-26"

# Общий фильтр исторической статистики рынка (правила ТЗ, Вопрос 2):
# только факты, простые маршруты, валидные цена/дистанция, без выбросов,
# без плановых месяцев из будущего.
MART_FILTER = """
    WHERE is_fact
      AND NOT complex
      AND NOT outlier_mark
      AND price_rub > 0
      AND distance_km > 0
      AND loading_date <= %(as_of)s::date
"""

MART_QUERIES = [
    ("mart.prices_monthly", f"""
        INSERT INTO mart.prices_monthly
        SELECT date_trunc('month', loading_date)::date,
               count(*),
               round(avg(price_rub)::numeric, 2),
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_rub)::numeric, 2),
               round(percentile_cont(0.9) WITHIN GROUP (ORDER BY price_rub)::numeric, 2)
        FROM core.prices_norm
        {MART_FILTER}
        GROUP BY 1
    """),
    ("mart.prices_by_weight", f"""
        INSERT INTO mart.prices_by_weight
        SELECT date_trunc('month', loading_date)::date,
               weight_category,
               count(*),
               round(avg(price_rub)::numeric, 2)
        FROM core.prices_norm
        {MART_FILTER}
        GROUP BY 1, 2
    """),
    ("mart.top_routes", f"""
        INSERT INTO mart.top_routes
        SELECT date_trunc('month', p.loading_date)::date,
               c1.full_name_gc,
               c2.full_name_gc,
               count(*),
               round(avg(p.price_rub)::numeric, 2)
        FROM core.prices_norm p
        JOIN core.cities_dedup c1 ON c1.id = p.loading_city_id
        JOIN core.cities_dedup c2 ON c2.id = p.unloading_city_id
        WHERE p.is_fact
          AND NOT p.complex
          AND NOT p.outlier_mark
          AND p.price_rub > 0
          AND p.distance_km > 0
          AND p.loading_date <= %(as_of)s::date
        GROUP BY 1, 2, 3
    """),
]

# ---------------------------------------------------------------------------
# Витрина "Карточка маршрута и коридор рыночных ставок" (ТЗ, Вопрос 2).
# Grain: месяц x город_отпр x город_назн x группа кузова x весовая категория.
# Месяц — из помесячной развертки core.price_dates (price_date); если таблица
# не перенесена в core, fallback на loading_date (с warning в лог).
# ---------------------------------------------------------------------------

CORRIDOR_MONTHLY_SRC = """
    SELECT date_trunc('month', pd.price_date)::date AS month,
           c1.gc_id                                                    AS loading_city_gcid,
           c2.gc_id                                                    AS unloading_city_gcid,
           p.body_type_group                                           AS body_group,
           p.weight_category,
           p.price_rub,
           p.price_rub / NULLIF(p.distance_km, 0)                      AS price_km,
           CASE WHEN p.price_type IN (3, 9) THEN p.organization_id END AS carrier_org
    FROM core.prices_norm p
    JOIN core.price_dates pd  ON pd.price_id = p.price_id
    JOIN core.cities_dedup c1 ON c1.id = p.loading_city_id
    JOIN core.cities_dedup c2 ON c2.id = p.unloading_city_id
    WHERE p.is_fact
      AND NOT p.complex
      AND NOT p.outlier_mark
      AND p.price_rub > 0
      AND p.distance_km > 0
      AND pd.price_date <= %(as_of)s::date
"""

CORRIDOR_FALLBACK_SRC = """
    SELECT date_trunc('month', p.loading_date)::date                   AS month,
           c1.gc_id                                                    AS loading_city_gcid,
           c2.gc_id                                                    AS unloading_city_gcid,
           p.body_type_group                                           AS body_group,
           p.weight_category,
           p.price_rub,
           p.price_rub / NULLIF(p.distance_km, 0)                      AS price_km,
           CASE WHEN p.price_type IN (3, 9) THEN p.organization_id END AS carrier_org
    FROM core.prices_norm p
    JOIN core.cities_dedup c1 ON c1.id = p.loading_city_id
    JOIN core.cities_dedup c2 ON c2.id = p.unloading_city_id
    WHERE p.is_fact
      AND NOT p.complex
      AND NOT p.outlier_mark
      AND p.price_rub > 0
      AND p.distance_km > 0
      AND p.loading_date <= %(as_of)s::date
"""

CORRIDOR_MAIN = """
    WITH base AS (
        {src}
    ),
    agg AS (
        SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category,
               count(*)                                                         AS trips_count,
               count(DISTINCT carrier_org)                                      AS carriers_count,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY price_rub)::numeric AS p25_trip,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY price_rub)::numeric AS median_trip,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY price_rub)::numeric AS p75_trip,
               avg(price_rub)::numeric                                          AS avg_trip,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY price_km)::numeric  AS p25_km,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY price_km)::numeric  AS median_km,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY price_km)::numeric  AS p75_km,
               CASE WHEN count(*) >= 2 AND avg(price_rub) > 0
                    THEN (stddev_samp(price_rub) / avg(price_rub))::numeric
               END                                                              AS cv_rate
        FROM base
        GROUP BY 1, 2, 3, 4, 5
    ),
    win AS (
        SELECT agg.*,
               max(month)       OVER w AS last_month,
               lag(median_trip) OVER w AS prev_median
        FROM agg
        WINDOW w AS (PARTITION BY loading_city_gcid, unloading_city_gcid,
                            body_group, weight_category ORDER BY month)
    )
    INSERT INTO mart.market_corridor
    SELECT w.month, w.loading_city_gcid, w.unloading_city_gcid,
           w.body_group, w.weight_category,
           w.trips_count, w.carriers_count,
           round(w.p25_trip, 2), round(w.median_trip, 2),
           round(w.p75_trip, 2), round(w.avg_trip, 2),
           round(w.p25_km, 4), round(w.median_km, 4), round(w.p75_km, 4),
           round(w.cv_rate, 4),
           -- mom_pct: NULL, если соседнего месяца нет или знаменатель 0
           CASE WHEN w.prev_median > 0
                THEN round(100.0 * (w.median_trip - w.prev_median) / w.prev_median, 2)
           END,
           -- yoy_pct: NULL, если ячейки год назад нет (строгое правило ТЗ)
           CASE WHEN y.median_trip > 0
                THEN round(100.0 * (w.median_trip - y.median_trip) / y.median_trip, 2)
           END,
           CASE WHEN w.trips_count >= 30 AND w.carriers_count >= 10
                THEN 'HIGH'
                WHEN w.trips_count >= 10 AND w.carriers_count >= 5 THEN 'MEDIUM'
                ELSE 'LOW_DATA'
           END
    FROM win w
    LEFT JOIN win y
      ON  y.loading_city_gcid   = w.loading_city_gcid
      AND y.unloading_city_gcid = w.unloading_city_gcid
      AND y.body_group          = w.body_group
      AND y.weight_category     = w.weight_category
      AND y.month               = w.month - INTERVAL '1 year'
"""


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS ok", (name,)
    ).fetchone()["ok"]


def _corridor_sql(conn):
    """SQL коридора или None, если витрина ещё не создана (DDL 004)."""
    if not _table_exists(conn, "mart.market_corridor"):
        log.warning("mart.market_corridor не найдена: прогоните "
                    "sql/migrations/004_mart_corridor_ddl.sql, витрина пропущена")
        return None
    if _table_exists(conn, "core.price_dates"):
        log.info("Коридор: месяц из core.price_dates (правило ТЗ)")
        return CORRIDOR_MAIN.format(src=CORRIDOR_MONTHLY_SRC)
    log.warning("core.price_dates не найдена: fallback на loading_date. "
                "Прогоните миграцию 005, чтобы соблюсти ТЗ.")
    return CORRIDOR_MAIN.format(src=CORRIDOR_FALLBACK_SRC)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with psycopg.connect(DWH_DSN) as conn:
        conn.row_factory = dict_row
        run_id = uuid.uuid4()

        bounds = conn.execute(
            "SELECT min(loading_date), max(loading_date) FROM core.prices_norm"
        ).fetchone()
        start_date, end_date = bounds["min"], bounds["max"]

        conn.execute(
            "INSERT INTO core.etl_runs (run_id, layer, status, start_date, end_date) "
            "VALUES (%s, 'mart', 'RUNNING', %s, %s)",
            (run_id, start_date, end_date))
        conn.commit()

        try:
            rows_read = conn.execute(
                "SELECT count(*) FROM core.prices_norm").fetchone()["count"]

            corridor = _corridor_sql(conn)
            queries = list(MART_QUERIES)
            if corridor:
                queries.append(("mart.market_corridor", corridor))

            params = {"as_of": AS_OF_DATE}
            stats = {}
            for name, sql in queries:
                if not _table_exists(conn, name):
                    log.warning("%s не найдена: прогоните DDL, витрина пропущена", name)
                    continue
                conn.execute(f"TRUNCATE TABLE {name}")
                cur = conn.execute(sql, params)
                stats[name] = cur.rowcount
                log.info("mart.%s: пересобрано %s строк", name.split('.')[-1], cur.rowcount)

            rows_loaded = sum(stats.values())
            conn.execute(
                "UPDATE core.etl_runs SET status='OK', finished_at=now(), "
                "rows_read=%s, rows_loaded=%s, rows_quarantined=0 WHERE run_id=%s",
                (rows_read, rows_loaded, run_id))
            conn.commit()
        except Exception:
            # Транзакция в состоянии aborted — откат обязателен до записи статуса,
            # иначе UPDATE упадёт с InFailedSqlTransaction.
            conn.rollback()
            conn.execute(
                "UPDATE core.etl_runs SET status='FAILED', finished_at=now() "
                "WHERE run_id=%s",
                (run_id,))
            conn.commit()
            raise

    print(f"run_id: {run_id}")
    print(f"rows_read: {rows_read}")
    for k, v in stats.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
