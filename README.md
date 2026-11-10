# Умная логистика: ETL-хранилище + ML-прогноз рыночной ставки перевозки

Тестовое задание Middle Data Engineer. Пайплайн переносит данные операционной
БД `deals_bigdata` (побережья грузоперевозок: сделки, ставки, города, курсы
валют) в аналитическое хранилище `dwh`, строит витрины и обучает модель
прогноза цены перевозки с сохранением прогнозов в БД.

## Архитектура

```
deals_bigdata (PostgreSQL 17, источник)          dwh (хранилище)
┌────────────────────────────┐   etl.run_etl    ┌──────────┐   etl.run_core  ┌──────────┐
│ cities, prices, price_body │ ───────────────► │  raw     │ ──────────────► │  core    │
│ _types, price_dates,       │  валидация,      │ (как в   │  нормализация,  │ prices_  │
│ exchange_rates, price_     │  копирование     │ источни- │  дедупликация   │ norm,    │
│ group_stats, participants  │                  │ ке)      │  валюты, DQ     │ cities_  │
└────────────────────────────┘                  └──────────┘                 │ dedup,   │
                                                              etl.run_mart   │ price_   │
                                          ┌──────────────────────────────┐   │ dates,   │
                                          │ mart: prices_monthly,        │◄──┤ price_   │
                                          │ prices_by_weight, top_routes,│   │ types    │
                                          │ market_corridor (коридор),   │   └──────────┘
                                          │ ml_price_predictions (ML)    │
                                          └──────────────▲───────────────┘
                                                         │ ml.run_ml
                                                         │ (CatBoost + Ridge + Tree,
                                                         │  batch inference)
```

Три слоя:

- **raw** — построчная копия таблиц источника (валидация и счётчики в `etl_runs`);
- **core** — нормализованные данные: `prices_norm` (цены в рублях по курсу,
  флаги DQ: `is_fact`, `complex`, `outlier_mark`, категории веса/объёма),
  `cities_dedup` (дедупликация городов по `gc_id`, координаты),
  `price_dates` (помесячная развертка долгосрочных заявок), `price_types`
  (справочник ролей);
- **mart** — витрины: агрегаты рынка, топ-маршруты, рыночный коридор,
  прогнозы ML.

## Обоснование стека

- **PostgreSQL 17** — задан условием (источник — PostgreSQL-база);
- **docker compose** — двухконтейнерная схема (БД + пайплайн): проект поднимается
  одной командой на чистой машине, без ручных правок; 
- **CatBoost** для прогноза цены: смешанные типы признаков (категории без
  энкодинга), устойчивость к выбросам, обучение на log-цели. Бейзлайны — Ridge
  (линейная модель со стандартизацией и ограниченным one-hot) и
  DecisionTree (интерпретируемость). Все три модели сравниваются на одном
  хронологическом сплите.

## Быстрый старт (чистая машина с Docker)

```bash
git clone <repo> && cd de-task
cp .env.example .env          # при желании поменяйте N_PRICES (объём синтетики)
docker compose up --build
docker compose logs -f pipeline
```

Первый запуск автоматически: создаёт базы `deals_bigdata` и `dwh`, применяет
`sql/schema.sql`, при первом старте применяет схему источника, генерирует синтетику
(детерминированную, ~300 000 сделок, задаётся `N_PRICES` в `.env`), накатывает
миграции, выполняет ETL (raw → core → витрины) и ML-прогон — вся SQL-инициализация
выполняется в контейнере `pipeline` (от root), поэтому права файлов на хосте не влияют
на запуск. Повторный запуск пропускает схему и синтетику по маркеру `init_done`
и повторяет миграции и ETL (всё идемпотентно). Готовность в логе: `PIPELINE_DONE`.


### Повторный запуск / идемпотентность (Вопрос 1)

Загрузки идемпотентны: raw-слои пишутся с дедупликацией, core — полная
пересборка нормализованных таблиц, витрины — `TRUNCATE + INSERT` в одной
транзакции. Повторный запуск любой команды выше безопасен.
Повторная загрузка за выбранный период: удалить из `core` записи периода и
перезапустить пайплайн — источник перечитает их из `raw`:

```sql
DELETE FROM core.prices_norm
WHERE loading_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31';
DELETE FROM core.price_dates WHERE price_id NOT IN (SELECT price_id FROM core.prices_norm);
```

```bash
.venv/bin/python -m etl.run_core && .venv/bin/python -m etl.run_mart
```

## Проверочные SQL

### Сводка ML-прогнозов (заполняется на test-периоде, где факт известен)

```sql
SELECT model_name,
       count(*)                    AS total,
       count(actual_price_rub)     AS with_fact,
       round(avg(ape_pct)::numeric, 2)        AS avg_ape_pct,
       round(sqrt(avg(abs_error_rub^2))::numeric, 0) AS rmse_rub
FROM mart.ml_price_predictions
GROUP BY 1
ORDER BY 1;
```

### Карточка маршрута и рыночный коридор (Вопрос 2)

```sql
SELECT month, trips_count, carriers_count,
       p25_trip_rub, median_trip_rub, p75_trip_rub, avg_trip_rub,
       p25_km_rub, median_km_rub, p75_km_rub, cv_rate,
       mom_pct, yoy_pct, reliability_level
FROM mart.market_corridor
WHERE loading_city_gcid = 1001          -- Москва
  AND unloading_city_gcid = 1002        -- Санкт-Петербург
  AND body_group = 'тент'
  AND weight_category = '20'
ORDER BY month DESC;
```

### Агрегаты рынка и топ-маршруты

```sql
SELECT * FROM mart.prices_monthly ORDER BY month;
SELECT * FROM mart.top_routes ORDER BY month DESC, deals DESC LIMIT 10;
```

### Целостность пайплайна

```sql
SELECT * FROM etl_runs ORDER BY started_at DESC LIMIT 5;
SELECT model_name, count(*), count(DISTINCT price_id)
FROM mart.ml_price_predictions GROUP BY 1;
```

## Методология ML (Вопрос 3)

- **Хронологический сплит**: train `[2026-07-26; 2026-10-05)`, test
  `[2026-10-05; 2026-10-26]` (as_of из ТЗ) — без утечки будущего;
- **Очистка**: IQR по `ln(price)` на train; выбросы `outlier_mark`;
- **Признаки**: дистанция, вес, объём, категории веса/объёма/кузова, города и
  координаты обеих точек, циклические календарные признаки (день недели, день
  года, месяц) — sin/cos;
- **Обучение**: CatBoost (600 деревьев, depth 8), Ridge (RidgeCV с подбором
  alpha, StandardScaler, one-hot с корзиной редких категорий), DecisionTree
  (depth 8); предсказание — в исходной шкале (exp от log-прогноза);
- **Batch inference**: прогнозы для test-периода (факт известен — пишутся
  `actual_price_rub`, `abs_error_rub`, `ape_pct`) и для заявок после as_of
  (факта нет). Каждый прогон помечается `run_id`, upsert по
  `(model_name, price_id)`.

## Метрики ML (прогон от 2026-11-10)

| Модель | R² | MAPE | RMSE, ₽ | MAE, ₽ |
|---|---|---|---|---|
| CatBoost (основная) | **0.8952** | 17.25% | 61 935 | 32 659 |
| DecisionTree | 0.8851 | 17.68% | 64 835 | 33 842 |
| Ridge (бейзлайн) | 0.7022 | 27.59% | 104 404 | 50 017 |

Топ-важностей CatBoost: `distance_km` (63.5%), `weight_t` (15.8%),
`weight_cat` (14.7%) — география и календарь дают < 3%.

## Структура репозитория

```
de-task/
├── docker-compose.yml      # postgres 17 + pipeline
├── Dockerfile              # образ пайплайна (python 3.12-slim + psql)
├── pipeline.sh             # полный цикл: ETL -> миграции -> витрины -> ML
├── .env.example            # переменные окружения
├── docker/init/01-init.sh  # создание БД deals_bigdata и dwh (первый старт тома)
├── sql/
│   ├── schema.sql          # DDL источника deals_bigdata
│   ├── seed/synthetic.sql  # детерминированный генератор данных
│   └── migrations/         # 001..006: витрины, ML-таблица, справочник,
│                           # коридор, помесячная развертка, прогнозы ML
├── etl/                    # run_etl (raw), run_core, run_mart, validate,
│                           # extract, load_raw, infer, ml_features, train, config
└── ml/                     # run_ml: feature pipeline, обучение, batch inference
```
