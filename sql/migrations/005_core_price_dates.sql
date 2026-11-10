-- 005_core_price_dates.sql
-- Помесячная развертка долгосрочных заявок (ТЗ, Вопрос 2:
-- месяц карточки маршрута формируется из price_dates.price_date).
-- Переносится только развертка сделок, прошедших DQ и доехавших до core,
-- поэтому JOIN по core.prices_norm, а не прямой перенос всего raw.
-- Идемпотентно: повторный запуск не плодит дубли (IF NOT EXISTS + ON CONFLICT).

CREATE TABLE IF NOT EXISTS core.price_dates (
    id         bigint      NOT NULL,
    price_id   bigint      NOT NULL,
    price_date date        NOT NULL,
    PRIMARY KEY (price_id, price_date)
);

CREATE INDEX IF NOT EXISTS ix_core_price_dates_date
    ON core.price_dates (price_date);

CREATE INDEX IF NOT EXISTS ix_core_price_dates_pid
    ON core.price_dates (price_id);

INSERT INTO core.price_dates (id, price_id, price_date)
SELECT pd.id, pd.price_id, pd.price_date
FROM raw.price_dates pd
JOIN core.prices_norm p ON p.price_id = pd.price_id
ON CONFLICT (price_id, price_date) DO NOTHING;
