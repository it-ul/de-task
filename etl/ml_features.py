"""Инженерия признаков для модели прогнозирования тарифа (Вопрос 3).

Источники: core.prices_norm (сделки) и core.cities_dedup (дедуплицированный
справочник городов, min(id) по gc_id — собран на слое core).

Правило anti-leakage: в признаках НЕТ price, price_rub, price_with_vat,
rate_value/rate_date, удельных тарифов (price/distance, price/weight),
коммерческих компонент и предрассчитанных агрегатов price_group_stats.
outlier_mark используется только для очистки train-выборки, как признак
не включается.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import psycopg

from .config import DWH_DSN

SQL_LOAD = """
SELECT p.price_id,
       p.loading_date,
       p.loading_city_gcid,
       p.unloading_city_gcid,
       p.distance_km,
       p.total_weight,
       p.total_volume,
       p.body_type_group AS body_type,
       p.weight_category,
       p.volume_category,
       p.is_fact,
       p.complex,
       p.outlier_mark,
       p.price_rub,
       o.full_name_local  AS origin_full,
       o.latitude         AS origin_lat,
       o.longitude        AS origin_lon,
       d.full_name_local  AS dest_full,
       d.latitude         AS dest_lat,
       d.longitude        AS dest_lon
FROM core.prices_norm p
JOIN core.cities_dedup o ON o.id = p.loading_city_id
JOIN core.cities_dedup d ON d.id = p.unloading_city_id
"""

CAT_FEATURES = [
    "body_type", "weight_category", "volume_category",
    "origin_city", "origin_region", "dest_city", "dest_region",
]
NUM_FEATURES = [
    "distance_km", "total_weight", "total_volume",
    "origin_lat", "origin_lon", "dest_lat", "dest_lon",
    "year", "month", "day_of_week", "day_of_year",
    "month_sin", "month_cos",
    "day_of_week_sin", "day_of_week_cos",
    "day_of_year_sin", "day_of_year_cos",
]
FEATURES = CAT_FEATURES + NUM_FEATURES


def split_location(full_name):
    """'Казань, Республика Татарстан' -> ('Казань', 'Республика Татарстан')."""
    if not full_name:
        return ("UNKNOWN", "UNKNOWN")
    parts = [s.strip() for s in str(full_name).split(",") if s.strip()]
    if len(parts) == 1:
        return (parts[0], "UNKNOWN")
    return (parts[0], ", ".join(parts[1:]))


def add_cyclical(df: pd.DataFrame) -> pd.DataFrame:
    """Год, месяц, день недели, день года + гармонические компоненты."""
    d = df["loading_date"]
    df["year"] = d.dt.year
    df["month"] = d.dt.month
    df["day_of_week"] = d.dt.dayofweek
    df["day_of_year"] = d.dt.dayofyear
    for col, period in (("month", 12.0), ("day_of_week", 7.0), ("day_of_year", 365.25)):
        df[f"{col}_sin"] = np.sin(2.0 * np.pi * df[col] / period)
        df[f"{col}_cos"] = np.cos(2.0 * np.pi * df[col] / period)
    return df


def to_bool(series: pd.Series) -> pd.Series:
    """Приведение булевых колонок (bool или текст) к pandas bool."""
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    s = series.astype("string").str.strip().str.lower()
    return s.isin(["t", "true", "1", "y", "yes"])


def outlier_mask(series: pd.Series) -> pd.Series:
    """True для строк-выбросов по outlier_mark (bool или текст)."""
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    s = series.astype("string").str.strip().str.lower()
    return s.isin(["t", "true", "1", "outlier"])


def build_features() -> pd.DataFrame:
    """Загружает сделки и собирает признаковое пространство без утечек."""
    with psycopg.connect(DWH_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_LOAD)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)

    # город + регион из полного наименования; регионы — кодируем раздельно
    for side, col in (("origin", "origin_full"), ("dest", "dest_full")):
        parsed = df[col].map(split_location)
        df[f"{side}_city"] = parsed.map(lambda t: t[0])
        df[f"{side}_region"] = parsed.map(lambda t: t[1])
        df.drop(columns=[col], inplace=True)

    df["loading_date"] = pd.to_datetime(df["loading_date"])
    add_cyclical(df)

    for c in CAT_FEATURES:
        df[c] = df[c].astype("string").fillna("UNKNOWN")
    return df
