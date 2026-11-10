"""ML-пайплайн прогноза цены перевозки (ТЗ, вопрос 4).

Подключение — как во всём проекте: DWH_DSN из etl.config, psycopg.
Данные: core.prices_norm + core.cities_dedup. Прогнозы: mart.ml_price_predictions.

Устойчивость к схеме: имена колонок читаются из information_schema, SQL
собирается по фактическим именам (кандидаты: price_rub/price,
body_type_group/body_group и т.п.), отсутствующие колонки -> NULL.

Запуск: .venv/bin/python -m ml.run_ml
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import date

import numpy as np
import pandas as pd
import psycopg
from catboost import CatBoostRegressor
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import Ridge, RidgeCV
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeRegressor

from etl.config import DWH_DSN

log = logging.getLogger("ml.run")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- Константы ТЗ -----------------------------------------------------------

AS_OF = date(2026, 10, 26)        # дата среза данных (ТЗ, вопрос 3)
TRAIN_START = date(2026, 7, 26)   # train: [TRAIN_START; TEST_START)
TEST_START = date(2026, 10, 5)    # test:  [TEST_START; AS_OF]
MODEL_NAME = "catboost_v1"

CAT_FEATURES = ["weight_cat", "volume_cat", "body_group",
                "origin_city", "dest_city"]
NUM_FEATURES = [
    "distance_km", "weight_t", "volume_m3",
    "origin_lat", "origin_lon", "dest_lat", "dest_lon",
    "dow_sin", "dow_cos", "doy_sin", "doy_cos", "month_sin", "month_cos",
]

# Кандидаты имён колонок core.prices_norm: берётся первая существующая.
CAND = {
    "price_id": ["price_id"],
    "loading_date": ["loading_date"],
    "price": ["price_rub", "price"],
    "distance": ["distance_km", "distance"],
    "total_weight": ["total_weight"],
    "total_volume": ["total_volume"],
    "weight_cat": ["weight_category"],
    "volume_cat": ["volume_category"],
    "body_group": ["body_type_group", "body_group"],
    "loading_city": ["loading_city_id"],
    "loading_city_gcid": ["loading_city_gcid"],
    "unloading_city": ["unloading_city_id"],
    "unloading_city_gcid": ["unloading_city_gcid"],
    "is_fact": ["is_fact"],
    "complex": ["complex"],
    "outlier_mark": ["outlier_mark"],
}
REQUIRED = ["price_id", "loading_date", "price", "distance",
            "loading_city", "unloading_city"]

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS mart.ml_price_predictions (
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
)
"""

UPSERT_SQL = """
INSERT INTO mart.ml_price_predictions
    (price_id, model_name, predicted_price_rub, run_id,
     loading_date, loading_city_gcid, unloading_city_gcid, body_group,
     actual_price_rub, abs_error_rub, ape_pct)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (model_name, price_id) DO UPDATE SET
    predicted_price_rub = EXCLUDED.predicted_price_rub,
    run_id              = EXCLUDED.run_id,
    loading_date        = EXCLUDED.loading_date,
    loading_city_gcid   = EXCLUDED.loading_city_gcid,
    unloading_city_gcid = EXCLUDED.unloading_city_gcid,
    body_group          = EXCLUDED.body_group,
    actual_price_rub    = EXCLUDED.actual_price_rub,
    abs_error_rub       = EXCLUDED.abs_error_rub,
    ape_pct             = EXCLUDED.ape_pct,
    created_at          = now()
"""


# --- Работа с БД ------------------------------------------------------------

def get_columns(conn, schema: str, table: str) -> list[str]:
    """Имена колонок таблицы. Работает при любой фабрике строк psycopg."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (schema, table))
        names = []
        for row in cur.fetchall():
            if isinstance(row, dict):
                names.append(row.get("column_name"))
            else:
                names.append(row[0])
        return [n for n in names if n]


def fetch_df(conn, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Читает результат запроса в DataFrame (курсор по умолчанию — кортежи)."""
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        names = [d.name for d in cur.description]
        rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                for r in cur.fetchall()]
    return pd.DataFrame(rows, columns=names)


def build_mapping(cols: list[str]) -> dict:
    """Для каждого логического поля — фактическое имя колонки или None."""
    return {k: next((n for n in v if n in cols), None) for k, v in CAND.items()}


def build_deals_sql(m: dict, future: bool) -> str:
    """SELECT по prices_norm с джойном городов. future=True — заявки после AS_OF."""

    def sel(alias: str, key: str) -> str:
        c = m.get(key)
        return f"p.{c} AS {alias}" if c else f"NULL AS {alias}"

    parts = [
        sel("price_id", "price_id"),
        sel("loading_date", "loading_date"),
        sel("price", "price"),
        sel("distance", "distance"),
        sel("total_weight", "total_weight"),
        sel("total_volume", "total_volume"),
        sel("weight_cat", "weight_cat"),
        sel("volume_cat", "volume_cat"),
        sel("body_group", "body_group"),
        sel("loading_city_id", "loading_city"),
        sel("unloading_city_id", "unloading_city"),
        sel("loading_city_gcid", "loading_city_gcid"),
        sel("unloading_city_gcid", "unloading_city_gcid"),
        "cl.full_name_gc AS origin_city",
        "cl.latitude  AS origin_lat",
        "cl.longitude AS origin_lon",
        "cu.full_name_gc AS dest_city",
        "cu.latitude  AS dest_lat",
        "cu.longitude AS dest_lon",
    ]

    filters = []
    if m.get("price"):
        filters.append(f"p.{m['price']} > 0")
    if m.get("distance"):
        filters.append(f"p.{m['distance']} > 0")
    # Фильтры фактичности — только для истории. Будущие заявки (future=True)
    # не являются фактами (is_fact = FALSE), безусловные фильтры вырезали бы их.
    if not future:
        if m.get("is_fact"):
            filters.append("p.is_fact")
        if m.get("complex"):
            filters.append("NOT p.complex")
        if m.get("outlier_mark"):
            filters.append("NOT p.outlier_mark")
    if m.get("loading_date"):
        if future:
            filters.append(f"p.{m['loading_date']} > %(as_of)s")
        else:
            filters.append(f"p.{m['loading_date']} >= %(train_start)s")
            filters.append(f"p.{m['loading_date']} <= %(as_of)s")

    sql = (
        "SELECT " + ",\n       ".join(parts) +
        "\nFROM core.prices_norm p"
        "\nLEFT JOIN core.cities_dedup cl ON cl.id = p." + str(m.get("loading_city")) +
        "\nLEFT JOIN core.cities_dedup cu ON cu.id = p." + str(m.get("unloading_city"))
    )
    if filters:
        sql += "\nWHERE " + "\n  AND ".join(filters)
    return sql


def ensure_table(conn) -> None:
    """Создаёт витрину прогнозов, если её нет; проверяет схему для upsert."""
    conn.execute(TABLE_DDL)
    cols = get_columns(conn, "mart", "ml_price_predictions")
    need = {"price_id", "model_name", "predicted_price_rub",
            "loading_date", "run_id", "created_at"}
    if not need.issubset(set(cols)):
        raise RuntimeError(
            f"Таблица mart.ml_price_predictions не совпадает по колонкам: {cols}. "
            "Запустите миграцию: psql -d dwh -f sql/migrations/006_ml_predictions.sql")
    has_uniq = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = 'ml_price_predictions_uniq' "
        "AND conrelid = 'mart.ml_price_predictions'::regclass").fetchone()
    if not has_uniq:
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ml_price_predictions_uniq "
                "ON mart.ml_price_predictions (model_name, price_id)")
            print("добавлен уникальный индекс (model_name, price_id)")
        except Exception:
            conn.rollback()
            raise RuntimeError(
                "В таблице прогнозов есть дубли по (model_name, price_id) — "
                "запустите миграцию 006 (DROP+CREATE) и повторите прогон.")


# --- Фичи и модели ----------------------------------------------------------

def _num(df: pd.DataFrame, key: str) -> pd.Series:
    col = df.get(key)
    if col is None:
        return pd.Series(np.nan, index=df.index)
    return pd.to_numeric(col, errors="coerce")


def _cat(df: pd.DataFrame, key: str) -> pd.Series:
    col = df.get(key)
    if col is None:
        return pd.Series("unknown", index=df.index, dtype="string")
    return col.astype("string").fillna("unknown")


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Матрица признаков. Месяц — 1-индексированный (июль -> 210 градусов)."""
    f = pd.DataFrame(index=df.index)
    f["distance_km"] = _num(df, "distance")
    f["weight_t"] = _num(df, "total_weight") / 1000.0
    f["volume_m3"] = _num(df, "total_volume")
    f["weight_cat"] = _cat(df, "weight_cat")
    f["volume_cat"] = _cat(df, "volume_cat")
    f["body_group"] = _cat(df, "body_group")
    f["origin_city"] = _cat(df, "origin_city")
    f["dest_city"] = _cat(df, "dest_city")
    for c in ("origin_lat", "origin_lon", "dest_lat", "dest_lon"):
        f[c] = _num(df, c)
    dt = pd.to_datetime(df["loading_date"], errors="coerce")
    f["dow_sin"] = np.sin(2 * np.pi * dt.dt.day_of_week / 7)
    f["dow_cos"] = np.cos(2 * np.pi * dt.dt.day_of_week / 7)
    f["doy_sin"] = np.sin(2 * np.pi * dt.dt.day_of_year / 365.25)
    f["doy_cos"] = np.cos(2 * np.pi * dt.dt.day_of_year / 365.25)
    f["month_sin"] = np.sin(2 * np.pi * dt.dt.month / 12)
    f["month_cos"] = np.cos(2 * np.pi * dt.dt.month / 12)
    return f


def _sk_pipeline(model, ridge: bool = False) -> Pipeline:
    """Категории -> one-hot (редкие значения уходят в корзину infrequent_sklearn),
    числа -> StandardScaler для линейной модели, passthrough для дерева.
    Ограничение one-hot защищает линейную модель от взрыва кардиналитета."""
    ohe = OneHotEncoder(handle_unknown="ignore",
                        min_frequency=10, max_categories=30)
    pre = ColumnTransformer(
        [("cat", ohe, CAT_FEATURES),
         ("num", StandardScaler() if ridge else "passthrough", NUM_FEATURES)])
    return Pipeline([("pre", pre), ("model", model)])


def metrics(y_true, y_pred) -> dict:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = np.abs(y_true)
    mape = float(np.mean(np.where(denom > 0,
                                  np.abs(y_true - y_pred) / np.where(denom > 0, denom, 1),
                                  np.nan))) * 100
    rmse = float(math.sqrt(mean_squared_error(y_true, y_pred)))
    return {
        "R2": round(float(r2_score(y_true, y_pred)), 4),
        "MAPE_pct": round(mape, 2),
        "RMSE_rub": round(rmse, 1),
        "MAE_rub": round(float(mean_absolute_error(y_true, y_pred)), 1),
    }


def _opt_int(v) -> int | None:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_text(v) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return str(v)


def _opt_price(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 2)


def build_prediction_rows(pairs, model, run_id) -> list:
    """Строки для upsert по расширенной схеме (002 + run_id).

    Для test-строк (есть факт) заполняются actual_price_rub, abs_error_rub,
    ape_pct; для future-заявок факта нет — эти колонки NULL.
    """
    rows = []
    for df, X in pairs:
        if not len(df):
            continue
        preds = np.exp(model.predict(X[CAT_FEATURES + NUM_FEATURES]))
        actual = (pd.to_numeric(df["price"], errors="coerce").to_numpy()
                  if "price" in df.columns
                  else np.full(len(df), np.nan))
        if "loading_date" in df.columns:
            dates = pd.to_datetime(df["loading_date"],
                                   errors="coerce").dt.date.to_numpy()
        else:
            dates = np.full(len(df), None, dtype=object)
        for i, pid in enumerate(df["price_id"].to_numpy()):
            if pid is None or (isinstance(pid, float) and math.isnan(pid)):
                continue
            pred = round(float(preds[i]), 2)
            act = _opt_price(actual[i])
            if act is not None and act > 0:
                abs_err = round(abs(pred - act), 2)
                ape = round(abs_err / act * 100, 4)
            else:
                abs_err = ape = None
            rows.append((
                int(pid), MODEL_NAME, pred, run_id, dates[i],
                _opt_int(df["loading_city_gcid"].iloc[i])
                if "loading_city_gcid" in df.columns else None,
                _opt_int(df["unloading_city_gcid"].iloc[i])
                if "unloading_city_gcid" in df.columns else None,
                _opt_text(df["body_group"].iloc[i])
                if "body_group" in df.columns else None,
                act, abs_err, ape,
            ))
    return rows


# --- Main -------------------------------------------------------------------

def _d(df: pd.DataFrame, agg: str) -> str:
    if not len(df):
        return "-"
    return getattr(df["loading_date"], agg)().date().isoformat()


def main() -> None:
    with psycopg.connect(DWH_DSN) as conn:
        cols_norm = get_columns(conn, "core", "prices_norm")
        cols_city = get_columns(conn, "core", "cities_dedup")
        print("core.prices_norm:", cols_norm)
        print("core.cities_dedup:", cols_city)

        m = build_mapping(cols_norm)
        missing = [k for k in REQUIRED if not m.get(k)]
        if missing:
            raise RuntimeError(
                f"В core.prices_norm нет обязательных колонок {missing}; "
                f"доступны: {cols_norm}")

        print("загрузка данных...")
        deals = fetch_df(conn, build_deals_sql(m, future=False),
                         {"train_start": TRAIN_START, "as_of": AS_OF})
        future = fetch_df(conn, build_deals_sql(m, future=True), {"as_of": AS_OF})
        row = conn.execute("SELECT count(*) FROM core.cities_dedup").fetchone()
        n_cities = row.get("count") if isinstance(row, dict) else row[0]
        print(f"города: {n_cities}")

    # Даты из БД приходят как datetime.date: конвертируем СРАЗУ,
    # до любых сравнений со сплитом (иначе TypeError).
    for frame in (deals, future):
        if "loading_date" in frame.columns:
            frame["loading_date"] = pd.to_datetime(frame["loading_date"])

    print(f"факты: {len(deals)}, будущие заявки: {len(future)}")

    train_mask = deals["loading_date"] < pd.Timestamp(TEST_START)
    train = deals[train_mask].copy()
    test = deals[~train_mask].copy()
    print(f"train: {len(train)} строк [{_d(train, 'min')} .. {TEST_START.isoformat()})")
    print(f"test:  {len(test)} строк [{TEST_START.isoformat()} .. {AS_OF.isoformat()}]")
    if train.empty:
        raise RuntimeError("Пустой train: проверьте TRAIN_START/AS_OF.")

    # IQR-очистка train по ln(цена)
    lnp = np.log(pd.to_numeric(train["price"], errors="coerce"))
    q1, q3 = float(lnp.quantile(0.25)), float(lnp.quantile(0.75))
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    keep = (lnp >= lo) & (lnp <= hi)
    print(f"IQR-очистка train: {len(train)} -> {int(keep.sum())} "
          f"(границы ln: {lo:.2f}..{hi:.2f})")
    train = train[keep]

    X_train = build_features(train)
    X_test = build_features(test)
    X_future = build_features(future)
    y_train = np.log(pd.to_numeric(train["price"], errors="coerce").to_numpy())

    med = X_train[NUM_FEATURES].median()
    for X in (X_train, X_test, X_future):
        X[NUM_FEATURES] = X[NUM_FEATURES].fillna(med)

    # CatBoost
    cat = CatBoostRegressor(iterations=600, depth=8, learning_rate=0.05,
                            loss_function="RMSE", random_seed=42, verbose=0)
    cat.fit(X_train[CAT_FEATURES + NUM_FEATURES], y_train, cat_features=CAT_FEATURES)

    # Ridge и DecisionTree на one-hot матрице
    ridge = _sk_pipeline(RidgeCV(alphas=np.logspace(-2, 3, 12)), ridge=True)
    tree = _sk_pipeline(DecisionTreeRegressor(max_depth=8, random_state=42))
    ridge.fit(X_train, y_train)
    tree.fit(X_train, y_train)

    y_test = pd.to_numeric(test["price"], errors="coerce").to_numpy()
    print("CatBoost test-метрики:",
          metrics(y_test, np.exp(cat.predict(X_test[CAT_FEATURES + NUM_FEATURES]))))
    print("Ridge:", metrics(y_test, np.exp(ridge.predict(X_test))))
    print("DecisionTree:", metrics(y_test, np.exp(tree.predict(X_test))))

    imp = sorted(zip(cat.feature_names_, map(float, cat.get_feature_importance())),
                 key=lambda t: -t[1])
    print("топ-10 важности:", [(n, round(v, 1)) for n, v in imp[:10]])

    # Батч-инференс
    run_id = uuid.uuid4()
    rows = build_prediction_rows([(test, X_test), (future, X_future)], cat, run_id)
    print(f"batch inference... записано прогнозов: {len(rows)} "
          f"(test {len(test)}, future {len(future)})")

    with psycopg.connect(DWH_DSN) as conn:
        ensure_table(conn)
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, rows)
        conn.commit()

    print(f"run_id: {run_id}")


if __name__ == "__main__":
    main()
