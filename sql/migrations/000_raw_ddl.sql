-- Слой raw в DWH (база dwh): снимок первоисточника deals_bigdata.
-- Источник никогда не изменяем — raw единственная точка входа данных в хранилище.
CREATE SCHEMA IF NOT EXISTS raw;

-- Реестр загрузок raw
CREATE TABLE IF NOT EXISTS raw.load_runs (
    run_id      uuid        PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    start_date  date,
    end_date    date,
    status      text        NOT NULL,   -- RUNNING / OK / FAILED
    rows_source bigint,
    rows_loaded bigint,
    details     jsonb
);

COMMENT ON TABLE raw.load_runs IS 'Реестр прогонов raw-загрузки (аудит идемпотентности)';

-- ==== Факт: окно по loading_date, DELETE + COPY в одной транзакции ====
CREATE TABLE IF NOT EXISTS raw.prices (
    id                  bigint PRIMARY KEY,
    created_at          timestamp(0),
    updated_at          timestamp(0),
    subscriber_id       integer,
    organization_id     integer,
    loading_date        date             NOT NULL,
    loading_city_gcid   integer          NOT NULL,
    unloading_city_gcid integer          NOT NULL,
    distance            double precision,
    total_weight        double precision,
    total_volume        double precision,
    body_type           integer,
    price               double precision,
    currency_code       varchar(3),
    complex             boolean,
    price_type          smallint,
    item_id             integer,
    is_fact             boolean,
    nearest_rate_date   date,
    meta_item_id        bigint,
    end_loading_date    date,
    source              varchar(255),
    external_item_uuid  uuid,
    outlier_mark        boolean,
    weight_category     varchar(4),
    volume_category     varchar(4),
    price_group_id      bigint,
    _run_id             uuid             NOT NULL,
    _loaded_at          timestamptz      NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_prices_loading_date ON raw.prices (loading_date);
CREATE INDEX IF NOT EXISTS ix_raw_prices_route ON raw.prices (loading_city_gcid, unloading_city_gcid);

COMMENT ON TABLE raw.prices IS 'Снимок факта сделок из deals_bigdata.public.prices по окну loading_date';

-- ==== Дочерние таблицы: грузим по множеству price_id окна ====
CREATE TABLE IF NOT EXISTS raw.price_body_types (
    id           bigint PRIMARY KEY,
    price_id     bigint          NOT NULL,
    body_type_id integer         NOT NULL,
    _run_id      uuid            NOT NULL,
    _loaded_at   timestamptz     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_pbt_pid ON raw.price_body_types (price_id);

CREATE TABLE IF NOT EXISTS raw.price_dates (
    id         bigint PRIMARY KEY,
    price_id   bigint      NOT NULL,
    price_date date        NOT NULL,
    _run_id    uuid        NOT NULL,
    _loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_price_dates_pid ON raw.price_dates (price_id);

CREATE TABLE IF NOT EXISTS raw.price_participants (
    id            bigint PRIMARY KEY,
    price_id      bigint      NOT NULL,
    subscriber_id bigint      NOT NULL,
    _run_id       uuid        NOT NULL,
    _loaded_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_pp_pid ON raw.price_participants (price_id);

-- ==== Справочники: полная перезагрузка каждый прогон (без _run_id) ====
CREATE TABLE IF NOT EXISTS raw.cities (
    id              bigint PRIMARY KEY,
    full_name_local varchar(255) NOT NULL,
    full_name_gc    varchar(255) NOT NULL,
    gc_id           integer      NOT NULL,
    name            varchar(255) NOT NULL,
    longitude       double precision,
    latitude        double precision,
    created_at      timestamp(0),
    updated_at      timestamp(0),
    _loaded_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.body_types (
    id           bigint PRIMARY KEY,
    name         varchar(255) NOT NULL,
    "id_from_1C" integer      NOT NULL,
    std_id       integer,
    _loaded_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.exchange_rates (
    id           bigint PRIMARY KEY,
    num_code     varchar(255)     NOT NULL,
    value        double precision NOT NULL,
    setting_date date             NOT NULL,
    _loaded_at   timestamptz      NOT NULL DEFAULT now()
);
