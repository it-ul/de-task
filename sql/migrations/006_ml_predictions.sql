-- 006: витрина прогнозов ML (ТЗ, вопрос 4).
-- Расширенная схема: наследует поля 002 (факт, ошибка, APE) + run_id и
-- уникальный ключ (model_name, price_id) для идемпотентных повторных прогонов.
-- Пересоздание идемпотентно: прогнозы полностью перезаписываются каждым
-- прогоном ml.run_ml (upsert по (model_name, price_id)).
DROP TABLE IF EXISTS mart.ml_price_predictions;

CREATE TABLE mart.ml_price_predictions (
    id                  bigserial      PRIMARY KEY,
    price_id            bigint         NOT NULL,
    loading_date        date           NOT NULL,
    loading_city_gcid   int,
    unloading_city_gcid int,
    body_group          text,
    actual_price_rub    numeric(12, 2),
    predicted_price_rub numeric(12, 2) NOT NULL,
    abs_error_rub       numeric(12, 2),
    ape_pct             numeric(10, 4),
    model_name          text           NOT NULL,
    run_id              uuid,
    created_at          timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT ml_price_predictions_uniq UNIQUE (model_name, price_id)
);

CREATE INDEX ix_ml_pred_model_price ON mart.ml_price_predictions (model_name, price_id);
CREATE INDEX ix_ml_pred_date        ON mart.ml_price_predictions (loading_date);

-- Проверка качества (заполняется на test-периоде, где факт известен):
-- SELECT model_name, count(*) AS total,
--        count(actual_price_rub) AS with_fact,
--        round(avg(ape_pct)::numeric, 2) AS avg_ape_pct
-- FROM mart.ml_price_predictions GROUP BY 1;
