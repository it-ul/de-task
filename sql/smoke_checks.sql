-- ============================================================
-- smoke_checks.sql — дымовые проверки витрин
-- Запуск: psql -h localhost -U postgres -d dwh -f sql/smoke_checks.sql
-- ============================================================

DROP TABLE IF EXISTS smoke_checks;
CREATE TABLE smoke_checks (n int, name text, fact text, expect text, status text);

-- === Блок A. mart.market_corridor: жёсткие инварианты ===

INSERT INTO smoke_checks
SELECT 1, 'месяцев в коридоре = месяцев в prices_monthly',
       (SELECT count(DISTINCT month)::text FROM mart.market_corridor),
       (SELECT count(*)::text FROM mart.prices_monthly),
       CASE WHEN (SELECT count(DISTINCT month) FROM mart.market_corridor)
                 = (SELECT count(*) FROM mart.prices_monthly)
            THEN 'PASS' ELSE 'FAIL' END;

INSERT INTO smoke_checks
SELECT 2, 'p25_trip <= median_trip <= p75_trip',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE NOT (p25_trip <= median_trip AND median_trip <= p75_trip);

INSERT INTO smoke_checks
SELECT 3, 'p25_km <= median_km <= p75_km',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE NOT (p25_km <= median_km AND median_km <= p75_km);

INSERT INTO smoke_checks
SELECT 4, 'carriers_count <= trips_count',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE carriers_count IS NOT NULL AND carriers_count > trips_count;

INSERT INTO smoke_checks
SELECT 5, 'нет ячеек с trips_count <= 0',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE trips_count <= 0;

INSERT INTO smoke_checks
SELECT 6, 'reliability_level соответствует порогам 30/10 и 10/5',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE reliability_level IS DISTINCT FROM
      CASE WHEN trips_count >= 30 AND carriers_count >= 10 THEN 'HIGH'
           WHEN trips_count >= 10 AND carriers_count >= 5  THEN 'MEDIUM'
           ELSE 'LOW_DATA' END;

-- Диагностика к проверке 6: раскладка расхождений (видна всегда,
-- при PASS в ней будет 0 строк — игнорируйте вывод)
SELECT reliability_level AS stored_level,
       CASE WHEN trips_count >= 30 AND carriers_count >= 10 THEN 'HIGH'
            WHEN trips_count >= 10 AND carriers_count >= 5  THEN 'MEDIUM'
            ELSE 'LOW_DATA' END AS recomputed_level,
       count(*) AS cells,
       min(trips_count) AS min_trips, max(trips_count) AS max_trips,
       min(carriers_count) AS min_carr, max(carriers_count) AS max_carr
FROM mart.market_corridor
GROUP BY 1, 2 ORDER BY cells DESC LIMIT 20;

INSERT INTO smoke_checks
SELECT 7, 'ключи (month, маршруты, кузов, вес) заполнены',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE month IS NULL
   OR loading_city_id IS NULL OR unloading_city_id IS NULL
   OR body_group IS NULL OR weight_category IS NULL;

INSERT INTO smoke_checks
SELECT 8, '|mom_pct| и |yoy_pct| <= 500% в MEDIUM/HIGH',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM mart.market_corridor
WHERE reliability_level IN ('MEDIUM', 'HIGH')
  AND (abs(mom_pct) > 500 OR abs(yoy_pct) > 500);

INSERT INTO smoke_checks
SELECT 9, 'cv_rate заполнен при trips_count >= 2 (допуск 20 ячеек)',
       count(*)::text, '<= 20',
       CASE WHEN count(*) <= 20 THEN 'PASS' ELSE 'WARN' END
FROM mart.market_corridor
WHERE trips_count >= 2 AND cv_rate IS NULL;

INSERT INTO smoke_checks
SELECT 10, 'sum(trips_count) >= строк в core (фильтры ТЗ)',
       (SELECT sum(trips_count)::text FROM mart.market_corridor),
       '>=' || (SELECT count(*)::text FROM core.prices_norm
                WHERE is_fact AND NOT complex AND NOT outlier_mark
                  AND price_rub > 0 AND distance_km > 0),
       CASE WHEN (SELECT sum(trips_count) FROM mart.market_corridor)
                 >= (SELECT count(*) FROM core.prices_norm
                     WHERE is_fact AND NOT complex AND NOT outlier_mark
                       AND price_rub > 0 AND distance_km > 0)
            THEN 'PASS' ELSE 'FAIL' END;

SELECT n AS "N", name AS "Проверка", fact AS "Факт", expect AS "Ожидание", status AS "Статус"
FROM smoke_checks ORDER BY n;

-- === Блок B. Остальные витрины: информационные сводные ===

SELECT 'mart.prices_monthly' AS mart, count(*) AS rows_,
       count(DISTINCT month) AS months, min(month) AS min_month, max(month) AS max_month
FROM mart.prices_monthly;

SELECT 'mart.prices_by_weight' AS mart, count(*) AS rows_,
       count(DISTINCT month) AS months, min(month) AS min_month, max(month) AS max_month
FROM mart.prices_by_weight;

SELECT 'mart.top_routes' AS mart, count(*) AS rows_,
       count(DISTINCT month) AS months, min(month) AS min_month, max(month) AS max_month
FROM mart.top_routes;

-- === Блок C. Итог smoke-прогона ===

SELECT CASE WHEN count(*) FILTER (WHERE status = 'FAIL') = 0
            THEN 'SMOKE: OK'
            ELSE 'SMOKE: FAIL — провалены проверки: ' ||
                 string_agg(n::text, ', ' ORDER BY n) FILTER (WHERE status = 'FAIL')
       END AS verdict
FROM smoke_checks;

DROP TABLE IF EXISTS smoke_checks;
