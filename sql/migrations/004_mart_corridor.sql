-- 004: витрина "Карточка маршрута и коридор рыночных ставок" (ТЗ, Вопрос 2).
-- Часть 1: DDL таблицы mart.market_corridor (grain: месяц x город_отпр x
-- город_назн x группа кузова x весовая категория).
-- Часть 2: эталонный расчёт (полная атомарная пересборка TRUNCATE + INSERT).
-- Правила ТЗ:
--   * только факты (is_fact), простые маршруты (complex = false), валидные цена/дистанция;
--   * выбросы (outlier_mark = true) исключены;
--   * месяц — из помесячной развертки core.price_dates (price_date);
--   * плановые месяцы из будущего (> as_of) не попадают в историческую статистику;
--   * carriers_count — только перевозчики (price_type IN (3, 9));
--   * cv_rate — NULL при менее чем 2 наблюдениях;
--   * mom_pct / yoy_pct — NULL, если соседней ячейки нет (без подстановки);
--   * reliability_level: HIGH при trips_count >= 30 и carriers_count >= 10;
--     MEDIUM при trips_count >= 10 и carriers_count >= 5; иначе LOW_DATA.
-- Тот же расчёт с fallback на loading_date выполняет etl/run_mart.py.

CREATE TABLE IF NOT EXISTS mart.market_corridor (
    month               date        NOT NULL,
    loading_city_gcid   int         NOT NULL,
    unloading_city_gcid int         NOT NULL,
    body_group          text,
    weight_category     varchar(4),
    trips_count         bigint      NOT NULL,
    carriers_count      bigint,
    p25_trip_rub        numeric,
    median_trip_rub     numeric,
    p75_trip_rub        numeric,
    avg_trip_rub        numeric,
    p25_km_rub          numeric,
    median_km_rub       numeric,
    p75_km_rub          numeric,
    cv_rate             numeric,
    mom_pct             numeric,
    yoy_pct             numeric,
    reliability_level   varchar(16) NOT NULL
);

BEGIN;

TRUNCATE TABLE mart.market_corridor;

WITH params AS (
    SELECT DATE '2026-10-26' AS as_of
),
base AS (
    SELECT date_trunc('month', pd.price_date)::date AS month,
           c1.gc_id AS loading_city_gcid,
           c2.gc_id AS unloading_city_gcid,
           p.body_type_group AS body_group,
           p.weight_category,
           p.price_rub,
           p.price_rub / NULLIF(p.distance_km, 0)         AS price_km,
           CASE WHEN p.price_type IN (3, 9) THEN p.organization_id END AS carrier_org
    FROM core.prices_norm p
    JOIN core.price_dates pd  ON pd.price_id = p.price_id
    JOIN core.cities_dedup c1 ON c1.id = p.loading_city_id
    JOIN core.cities_dedup c2 ON c2.id = p.unloading_city_id
    CROSS JOIN params
    WHERE p.is_fact
      AND NOT p.complex
      AND NOT p.outlier_mark
      AND p.price_rub > 0
      AND p.distance_km > 0
      AND pd.price_date <= params.as_of
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
           lag(median_trip) OVER w AS prev_median
    FROM agg
    WINDOW w AS (PARTITION BY loading_city_gcid, unloading_city_gcid,
                        body_group, weight_category ORDER BY month)
),
yoy AS (
    SELECT w.*,
           y.median_trip AS yoy_median
    FROM win w
    LEFT JOIN win y
      ON  y.loading_city_gcid   = w.loading_city_gcid
      AND y.unloading_city_gcid = w.unloading_city_gcid
      AND y.body_group          = w.body_group
      AND y.weight_category     = w.weight_category
      AND y.month               = w.month - INTERVAL '1 year'
)
INSERT INTO mart.market_corridor
SELECT y.month, y.loading_city_gcid, y.unloading_city_gcid,
       y.body_group, y.weight_category,
       y.trips_count, y.carriers_count,
       round(y.p25_trip, 2), round(y.median_trip, 2),
       round(y.p75_trip, 2), round(y.avg_trip, 2),
       round(y.p25_km, 4), round(y.median_km, 4), round(y.p75_km, 4),
       round(y.cv_rate, 4),
       -- mom_pct: NULL, если соседнего месяца нет или знаменатель 0
       CASE WHEN y.prev_median > 0
            THEN round(100.0 * (y.median_trip - y.prev_median) / y.prev_median, 2)
       END,
       -- yoy_pct: NULL, если ячейки год назад нет (окно данных < года)
       CASE WHEN y.yoy_median > 0
            THEN round(100.0 * (y.median_trip - y.yoy_median) / y.yoy_median, 2)
       END,
       CASE WHEN y.trips_count >= 30 AND y.carriers_count >= 10
            THEN 'HIGH'
            WHEN y.trips_count >= 10 AND y.carriers_count >= 5 THEN 'MEDIUM'
            ELSE 'LOW_DATA'
       END
FROM yoy y;

COMMIT;
