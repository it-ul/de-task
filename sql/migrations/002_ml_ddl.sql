-- Витрина прогнозов модели (Вопрос 3, Batch Inference)
-- Пересоздание таблицы безопасно: скоринг перезаписывает строки по model_name.

DROP TABLE IF EXISTS mart.ml_price_predictions;

CREATE TABLE mart.ml_price_predictions (
    id                  bigserial PRIMARY KEY,
    price_id            bigint NOT NULL,
    loading_date        date NOT NULL,
    loading_city_gcid   int NOT NULL,
    unloading_city_gcid int NOT NULL,
    body_group          text,
    actual_price_rub    numeric(12, 2),
    predicted_price_rub numeric(12, 2) NOT NULL,
    abs_error_rub       numeric(12, 2),
    ape_pct             numeric(10, 4),
    model_name          text NOT NULL,
    scored_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_ml_pred_model_price ON mart.ml_price_predictions (model_name, price_id);
CREATE INDEX ix_ml_pred_date        ON mart.ml_price_predictions (loading_date);



SELECT model_name, count(*) AS total,
       count(actual_price_rub) AS with_fact,
       count(*) - count(actual_price_rub) AS future_no_fact,
       round(avg(ape_pct)::numeric, 2) AS avg_ape_pct
FROM mart.ml_price_predictions GROUP BY 1;