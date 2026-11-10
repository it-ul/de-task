-- Витрина "Карточка маршрута и коридор рыночных ставок" (ТЗ, Вопрос 2).
-- Grain: месяц x город_отпр x город_назн x группа кузова x весовая категория.
-- Города хранятся как cities_dedup.id (дедупликация gc_id выполнена в core).

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.market_corridor (
    month                date         NOT NULL,  -- месяц из price_dates.price_date (развертка)
    loading_city_id      integer      NOT NULL,  -- FK: core.cities_dedup.id (отправление)
    unloading_city_id    integer      NOT NULL,  -- FK: core.cities_dedup.id (назначение)
    body_group           varchar(32)  NOT NULL,  -- стандартизированная группа кузова по std_id
    weight_category      varchar(16)  NOT NULL,  -- категория тоннажа (G20T/FTL/10T/5T/L5T/OTH)
    trips_count          integer,                -- число уникальных сделок в ячейке
    carriers_count       integer,                -- уникальные перевозчики (price_type IN (3, 9))
    p25_trip             numeric,                -- коридор за рейс: P25, RUB без НДС
    median_trip          numeric,                -- коридор за рейс: медиана (P50)
    p75_trip             numeric,                -- коридор за рейс: P75
    avg_trip             numeric,                -- коридор за рейс: среднее
    p25_km               numeric,                -- коридор за км: P25 (price_rub / distance_km)
    median_km            numeric,                -- коридор за км: медиана (P50)
    p75_km               numeric,                -- коридор за км: P75
    cv_rate              numeric,                -- коэффициент вариации: stddev_samp / avg; NULL при < 2 набл.
    mom_pct              numeric,                -- изменение медианы к предыдущему месяцу, %; NULL если ячейки нет
    yoy_pct              numeric,                -- изменение медианы к тому же месяцу прошлого года, %; NULL если ячейки нет
    reliability_level    varchar(16),            -- HIGH / MEDIUM / LOW_DATA (пороги 30/10 + свежесть 30 дней)
    PRIMARY KEY (month, loading_city_id, unloading_city_id, body_group, weight_category)
);

CREATE INDEX IF NOT EXISTS idx_market_corridor_cities
    ON mart.market_corridor (loading_city_id, unloading_city_id);
CREATE INDEX IF NOT EXISTS idx_market_corridor_reliability
    ON mart.market_corridor (reliability_level);
