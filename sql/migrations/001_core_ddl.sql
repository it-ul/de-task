
-- Слой core хранилища dwh: нормализованные цены, дедуп городов, карантин DQ, реестр прогонов.
CREATE SCHEMA IF NOT EXISTS core;

-- Реестр прогонов core
CREATE TABLE core.etl_runs (
    run_id           uuid PRIMARY KEY,
    layer            text NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    start_date       date,
    end_date         date,
    rows_read        bigint,
    rows_loaded      bigint,
    rows_quarantined bigint,
    status           text
);

-- Дедуп справочника городов: одна строка на gc_id (побеждает минимальный id)
CREATE TABLE core.cities_dedup (
    id              bigint PRIMARY KEY,
    gc_id           integer NOT NULL,
    name            varchar(255) NOT NULL,
    full_name_local varchar(255) NOT NULL,
    full_name_gc    varchar(255) NOT NULL,
    longitude       double precision,
    latitude        double precision,
    rebuilt_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_core_cities_dedup_gc ON core.cities_dedup (gc_id);

-- Карантин: отбракованные строки с кодом нарушения (первое правило по приоритету)
CREATE TABLE core.dq_quarantine_prices (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    price_id        bigint NOT NULL,
    quarantine_code text NOT NULL,
    reason          text,
    price_snapshot  jsonb,
    run_id          uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (price_id, quarantine_code)
);
CREATE INDEX ix_dq_quarantine_code ON core.dq_quarantine_prices (quarantine_code);

-- Нормализованный факт: партиции по месяцам loading_date
CREATE TABLE core.prices_norm (
    price_id            bigint NOT NULL,
    loading_date        date NOT NULL,
    created_at          timestamp(0),
    subscriber_id       integer,
    organization_id     integer,
    loading_city_id     bigint,
    unloading_city_id   bigint,
    loading_city_gcid   integer NOT NULL,
    unloading_city_gcid integer NOT NULL,
    distance_km         double precision,
    total_weight        double precision,
    total_volume        double precision,
    weight_category     varchar(4) NOT NULL,
    volume_category     varchar(4) NOT NULL,
    body_type_group     varchar(16) NOT NULL,
    body_type_ids       integer[],
    price_src           double precision,
    currency_code       varchar(3) NOT NULL,
    rate_date           date,
    rate_value          double precision,
    price_rub           double precision,
    complex             boolean,
    price_type          smallint,
    is_fact             boolean,
    outlier_mark        boolean,
    source              varchar(255),
    run_id              uuid NOT NULL,
    loaded_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (price_id, loading_date)
) PARTITION BY RANGE (loading_date);

CREATE OR REPLACE FUNCTION core.ensure_partition(p_date date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    p_start date := date_trunc('month', p_date)::date;
    p_end   date := (p_start + interval '1 month')::date;
    p_name  text := 'prices_norm_' || to_char(p_start, 'YYYY_MM');
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'core' AND c.relname = p_name
    ) THEN
        EXECUTE format(
            'CREATE TABLE core.%I PARTITION OF core.prices_norm FOR VALUES FROM (%L) TO (%L)',
            p_name, p_start, p_end);
    END IF;
END $$;

CREATE INDEX ix_prices_norm_route ON core.prices_norm (loading_city_gcid, unloading_city_gcid, loading_date);
CREATE INDEX ix_prices_norm_wv ON core.prices_norm (weight_category, volume_category);
