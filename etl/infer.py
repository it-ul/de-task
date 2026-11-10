"""Batch Inference: скоринг отложенного тестового окна и будущих заявок.

Скорятся все сделки с loading_date >= 2026-10-05:
  - отложенный test-период [2026-10-05, 2026-10-26];
  - плановые будущие заявки (loading_date > 2026-10-26), присутствующие
    в датасете (is_fact = false).

Для сделок с фактом (is_fact=true, complex=false, loading_date <= as_of,
price_rub > 0) заполняются actual_price_rub, abs_error_rub и ape_pct.
Для будущих заявок все три поля остаются NULL, даже если в строке заполнено
поле price — факта на дату скоринга ещё нет.

Идемпотентность: перед вставкой удаляются строки текущего model_name.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import psycopg
from catboost import CatBoostRegressor

from . import ml_features
from .config import DWH_DSN
from .train import AS_OF, META_FILE, MODEL_DIR, MODEL_FILE, MODEL_NAME, TEST_START
from .ml_features import to_bool

INSERT_SQL = """
INSERT INTO mart.ml_price_predictions
    (price_id, loading_date, loading_city_gcid, unloading_city_gcid,
     body_group, actual_price_rub, predicted_price_rub,
     abs_error_rub, ape_pct, model_name, scored_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
"""


def _num(x):
    return None if pd.isna(x) else round(float(x), 2)


def _ape(x):
    return None if pd.isna(x) else round(float(x), 4)


def main():
    meta = json.loads(META_FILE.read_text("utf-8"))
    model = CatBoostRegressor()
    model.load_model(str(MODEL_FILE))

    print("Загрузка признаков из core...")
    df = ml_features.build_features()
    scored = df[df["loading_date"] >= TEST_START].copy()
    print(f"Скоринг: {len(scored)} строк (loading_date >= {TEST_START.date()})")

    scored["predicted_price_rub"] = model.predict(scored[meta["features"]])

    # факт есть только у закрытых сделок test-периода; будущим заявкам — NULL
    has_actual = (
        to_bool(scored["is_fact"])
        & ~to_bool(scored["complex"])
        & (scored["loading_date"] <= AS_OF)
        & (scored["price_rub"] > 0)
    )
    actual = np.where(has_actual, scored["price_rub"], np.nan)
    err = np.abs(actual - scored["predicted_price_rub"].to_numpy())
    ape = err / actual * 100.0

    rows = [
        (
            int(r["price_id"]),
            r["loading_date"].date(),
            int(r["loading_city_gcid"]),
            int(r["unloading_city_gcid"]),
            None if pd.isna(r["body_type"]) else str(r["body_type"]),
            _num(actual[i]),
            round(float(r["predicted_price_rub"]), 2),
            _num(err[i]),
            _ape(ape[i]),
            MODEL_NAME,
        )
        for i, (_, r) in enumerate(scored.iterrows())
    ]

    with psycopg.connect(DWH_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mart.ml_price_predictions WHERE model_name = %s",
                (MODEL_NAME,),
            )
            cur.executemany(INSERT_SQL, rows)
        conn.commit()

    n_act = int(has_actual.sum())
    print(f"Записано в mart.ml_price_predictions: {len(rows)} (model_name={MODEL_NAME})")
    print(f"  с фактом (test-период):   {n_act}")
    print(f"  без факта (будущие):      {len(rows) - n_act}")

    if n_act:
        m = scored[has_actual.to_numpy()]
        err_act = np.abs(m["predicted_price_rub"].to_numpy() - m["price_rub"].to_numpy())
        print(
            f"  факт vs прогноз: MAE={err_act.mean():.0f} руб., "
            f"MAPE={(err_act / m['price_rub'] * 100.0).mean():.2f}%"
        )


if __name__ == "__main__":
    main()
