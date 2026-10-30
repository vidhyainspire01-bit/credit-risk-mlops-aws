"""Training step (runs inside the project image as `train`).

- XGBoost with monotonic constraints from credit_features
- Isotonic calibration fitted on the validation set
- Writes model.ubj (not pickle), calibrator.json and metadata.json to /opt/ml/model
"""

import json
import os

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

from credit_features import FEATURE_ORDER, MONOTONE_CONSTRAINTS

PREFIX = "/opt/ml"


def load(channel: str):
    df = pd.read_csv(f"{PREFIX}/input/data/{channel}/{channel}.csv", header=None)
    return df.iloc[:, 1:].to_numpy(dtype=np.float32), df.iloc[:, 0].to_numpy()


def main() -> None:
    hp = json.load(open(f"{PREFIX}/input/config/hyperparameters.json"))
    X_tr, y_tr = load("train")
    X_va, y_va = load("validation")

    w_path = f"{PREFIX}/input/data/train/train_weights.csv"
    weights = pd.read_csv(w_path, header=None)[0].to_numpy() if os.path.exists(w_path) else None

    sel_path = f"{PREFIX}/input/data/train/selected_features.json"
    features = json.load(open(sel_path)) if os.path.exists(sel_path) else FEATURE_ORDER
    idx = [FEATURE_ORDER.index(f) for f in features]
    X_tr, X_va = X_tr[:, idx], X_va[:, idx]

    dtr = xgb.DMatrix(X_tr, label=y_tr, weight=weights, feature_names=features, missing=np.nan)
    dva = xgb.DMatrix(X_va, label=y_va, feature_names=features, missing=np.nan)
    params = {
        "objective": "binary:logistic",
        "eval_metric": hp.get("eval_metric", "auc"),
        "max_depth": int(hp.get("max_depth", 4)),
        "eta": float(hp.get("eta", 0.05)),
        "subsample": float(hp.get("subsample", 0.8)),
        "colsample_bytree": float(hp.get("colsample_bytree", 0.8)),
        "min_child_weight": float(hp.get("min_child_weight", 20)),
        "monotone_constraints": "(" + ",".join(str(MONOTONE_CONSTRAINTS[f]) for f in features) + ")",
        "tree_method": "hist",
    }
    booster = xgb.train(
        params,
        dtr,
        num_boost_round=int(hp.get("num_round", 400)),
        evals=[(dva, "validation")],
        early_stopping_rounds=30,
        verbose_eval=50,
    )

    best_iter = int(booster.best_iteration)
    raw_va = booster.predict(dva, iteration_range=(0, best_iter + 1))
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0005, y_max=0.9995).fit(raw_va, y_va)
    print(f"validation:auc={roc_auc_score(y_va, raw_va):.4f}")

    os.makedirs(f"{PREFIX}/model", exist_ok=True)
    booster = booster[: best_iter + 1]  # keep only the trees up to the early-stopping point
    booster.save_model(f"{PREFIX}/model/model.ubj")
    json.dump(
        {"x": iso.X_thresholds_.tolist(), "y": iso.y_thresholds_.tolist()}, open(f"{PREFIX}/model/calibrator.json", "w")
    )
    json.dump(
        {"feature_order": features, "best_iteration": best_iter, "xgboost_version": xgb.__version__},
        open(f"{PREFIX}/model/metadata.json", "w"),
    )


if __name__ == "__main__":
    main()
