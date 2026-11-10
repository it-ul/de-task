"""Обучение CatBoost и бейзлайнов (Вопрос 3, часть 2).

Split строго хронологический (Time-Based Split), границы зафиксированы
снимком as_of из ТЗ, а не системной датой запуска:
    train: [2026-07-26, 2026-10-05)
    test:  [2026-10-05, 2026-10-26]
Граница 2026-10-05 входит только в test; shuffle=True недопустим.

Валидация для ранней остановки бустинга — последние 2 недели train
(хронологически позже остального train), тест в обучении не участвует.

Очистка: outlier_mark (базовая реализация) + опциональный IQR по
ln(price_rub); границы IQR и весь препроцессинг (imputer, scaler, one-hot)
подгоняются СТРОГО на train-выборке.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor, Pool
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeRegressor

from . import ml_features
#from ml_features import CAT_FEATURES, FEATURES, NUM_FEATURES, build_features, outlier_mask, to_bool
from .ml_features import CAT_FEATURES, FEATURES, NUM_FEATURES, build_features, outlier_mask, to_bool

AS_OF = pd.Timestamp("2026-10-26")
TRAIN_START = AS_OF - pd.Timedelta(days=92)      # 2026-07-26 (включительно)
TEST_START = pd.Timestamp("2026-10-05")          # входит только в test
VAL_START = TEST_START - pd.Timedelta(days=14)   # последние 2 недели train

MODEL_DIR = Path(__file__).resolve().parent.parent / "ml"
MODEL_FILE = MODEL_DIR / "catboost_base_v1.cbm"
META_FILE = MODEL_DIR / "meta.json"
MODEL_NAME = "catboost_base_v1"

USE_IQR = False  # базовая реализация: очистка только по outlier_mark


def prepare_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """is_fact=true, complex=false, положительные цена и км, чистка выбросов."""
    ds = df[to_bool(df["is_fact"]) & ~to_bool(df["complex"])].copy()
    ds = ds[(ds["price_rub"] > 0) & (ds["distance_km"] > 0)]
    ds = ds[~outlier_mask(ds["outlier_mark"])]

    if USE_IQR:
        tr = ds[(ds["loading_date"] >= TRAIN_START) & (ds["loading_date"] < TEST_START)]
        lp = np.log(tr["price_rub"])
        q1, q3 = lp.quantile(0.25), lp.quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        ds = ds[np.log(ds["price_rub"]).between(lo, hi)]
    return ds


def eval_metrics(y, p):
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    err = p - y
    return {
        "R2": round(float(r2_score(y, p)), 4),
        "MAPE_pct": round(float(np.mean(np.abs(err / y)) * 100.0), 2),
        "RMSE_rub": round(float(math.sqrt(mean_squared_error(y, p))), 2),
        "MAE_rub": round(float(np.mean(np.abs(err))), 2),
    }


def make_preprocessor() -> ColumnTransformer:
    """One-hot + imputer + scaler. Подгоняется только на train (fit на train)."""
    return ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_FEATURES),
        ("num", Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]), NUM_FEATURES),
    ])


def main():
    MODEL_DIR.mkdir(exist_ok=True)

    print("Загрузка признаков из core...")
    df = build_features()
    ds = prepare_dataset(df)

    train_all = ds[(ds["loading_date"] >= TRAIN_START) & (ds["loading_date"] < TEST_START)]
    val = train_all[train_all["loading_date"] >= VAL_START]
    train_core = train_all[train_all["loading_date"] < VAL_START]
    test = ds[(ds["loading_date"] >= TEST_START) & (ds["loading_date"] <= AS_OF)]

    print(f"train: {len(train_core)} сделок [{TRAIN_START.date()} - {VAL_START.date()})")
    print(f"val:   {len(val)} сделок  [{VAL_START.date()} - {TEST_START.date()})")
    print(f"test:  {len(test)} сделок  [{TEST_START.date()} - {AS_OF.date()}]")

    X_tr, y_tr = train_core[FEATURES], train_core["price_rub"]
    X_va, y_va = val[FEATURES], val["price_rub"]
    X_te, y_te = test[FEATURES], test["price_rub"]

    results = {}

    # 1. CatBoostRegressor — основная модель (нативные категории, без масштабирования)
    cat = CatBoostRegressor(
        iterations=800, learning_rate=0.05, depth=6,
        loss_function="RMSE", random_seed=42, verbose=200,
    )
    cat.fit(
        Pool(X_tr, y_tr, cat_features=CAT_FEATURES),
        eval_set=Pool(X_va, y_va, cat_features=CAT_FEATURES),
        early_stopping_rounds=100,
    )
    results[MODEL_NAME] = eval_metrics(y_te, cat.predict(X_te))

    # 2. Ridge — линейный бейзлайн
    linear = Pipeline([
        ("prep", make_preprocessor()),
        ("model", Ridge(alpha=1.0)),
    ])
    linear.fit(X_tr, y_tr)
    results["Ridge"] = eval_metrics(y_te, linear.predict(X_te))

    # 3. DecisionTree — деревянный бейзлайн
    tree = Pipeline([
        ("prep", make_preprocessor()),
        ("model", DecisionTreeRegressor(max_depth=8, min_samples_leaf=20, random_state=42)),
    ])
    tree.fit(X_tr, y_tr)
    results["DecisionTreeRegressor"] = eval_metrics(y_te, tree.predict(X_te))

    print("\nМетрики на test [" + str(TEST_START.date()) + " - " + str(AS_OF.date()) + "]:")
    print(f"{'model':<26} {'R2':>8} {'MAPE%':>8} {'RMSE,руб':>12} {'MAE,руб':>12}")
    for name, m in results.items():
        print(f"{name:<26} {m['R2']:>8.4f} {m['MAPE_pct']:>7.2f} {m['RMSE_rub']:>12.2f} {m['MAE_rub']:>12.2f}")

    imp = cat.get_feature_importance()
    top = sorted(zip(FEATURES, imp), key=lambda t: -t[1])[:10]
    print("\nТоп-10 важностей признаков (CatBoost):")
    for name, v in top:
        print(f"  {name:<20} {v:5.1f}%")

    meta = {
        "model_name": MODEL_NAME,
        "as_of": str(AS_OF.date()),
        "train": [str(TRAIN_START.date()), str(TEST_START.date())],
        "test": [str(TEST_START.date()), str(AS_OF.date())],
        "features": FEATURES,
        "cat_features": CAT_FEATURES,
        "metrics_on_test": results,
    }
    cat.save_model(str(MODEL_FILE))
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    print(f"\nСохранено: {MODEL_FILE.name}, meta.json в {MODEL_DIR}")


if __name__ == "__main__":
    main()
