-- Справочник ролей price_type (ТЗ, раздел "Справочник ролей")
CREATE TABLE IF NOT EXISTS core.price_types (
    price_type  smallint      PRIMARY KEY,
    type_name   varchar(64)   NOT NULL,
    org_role    varchar(64),
    is_fact     boolean       NOT NULL,
    created_at  timestamptz   NOT NULL DEFAULT now()
);

INSERT INTO core.price_types (price_type, type_name, org_role, is_fact) VALUES
    (1, 'Заявка заказчика',        'Грузовладелец',              true),
    (2, 'Заявка экспедитора',      'Экспедитор',                 true),
    (3, 'Заявка перевозчика',      'Перевозчик',                 true),
    (4, 'Историческая закрытая',   NULL,                         true),
    (5, 'Историческая закрытая',   NULL,                         true),
    (6, 'Ставка торгов/аукциона',  'Перевозчик (участник торгов)', false),
    (9, 'Прямой заказ',            'Перевозчик',                 true)
ON CONFLICT (price_type) DO UPDATE
SET type_name = EXCLUDED.type_name,
    org_role  = EXCLUDED.org_role,
    is_fact   = EXCLUDED.is_fact;





