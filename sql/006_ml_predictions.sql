-- 006: витрина ML-прогнозов (ТЗ, Вопрос 4).
-- Прогнозы пересчитываются каждым прогоном run_ml, поэтому таблицу можно
-- пересоздавать: скрипт идемпотентный и гарантирует актуальную схему
-- с уникальным ключом (price_id, model_name) для ON CONFLICT upsert.
DROP TABLE IF EXISTS mart.ml_price_predictions;

CREATE TABLE mart.ml_price_predictions (
    price_id        bigint      NOT NULL,
    model_name      text        NOT NULL,
    loading_date    date,
    origin_gc_id    bigint,
    dest_gc_id      bigint,
    distance_km     numeric(12, 2),
    price_rub_fact  numeric(12, 2),
    pred_price_rub  numeric(12, 2),
    is_future       boolean     NOT NULL DEFAULT false,
    run_id          uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mlpp_price_model_key UNIQUE (price_id, model_name)
);

CREATE INDEX mlpp_run_idx ON mart.ml_price_predictions (run_id);
