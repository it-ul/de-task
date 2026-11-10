-- =============================================================
-- 001_mart_ddl.sql — витрины слоя mart (полная пересборка за прогон)
-- =============================================================
CREATE SCHEMA IF NOT EXISTS mart;

-- Агрегаты рынка по месяцам
DROP TABLE IF EXISTS mart.prices_monthly CASCADE;
CREATE TABLE mart.prices_monthly (
    month            date PRIMARY KEY,
    deals            bigint  NOT NULL,
    avg_price_rub    numeric NOT NULL,
    median_price_rub numeric NOT NULL,
    p90_price_rub    numeric NOT NULL
);

-- Помесячная разбивка по категориям веса
DROP TABLE IF EXISTS mart.prices_by_weight CASCADE;
CREATE TABLE mart.prices_by_weight (
    month          date NOT NULL,
    weight_category text NOT NULL,
    deals          bigint NOT NULL,
    avg_price_rub  numeric NOT NULL,
    PRIMARY KEY (month, weight_category)
);

-- Направления город -> город по месяцам
DROP TABLE IF EXISTS mart.top_routes CASCADE;
CREATE TABLE mart.top_routes (
    month          date NOT NULL,
    loading_city   text NOT NULL,
    unloading_city text NOT NULL,
    deals          bigint NOT NULL,
    avg_price_rub  numeric NOT NULL,
    PRIMARY KEY (month, loading_city, unloading_city)
);
