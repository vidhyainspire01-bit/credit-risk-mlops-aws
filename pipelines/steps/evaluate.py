"""Evaluation step: out-of-time metrics + fairness check.

Writes /opt/ml/processing/evaluation/evaluation.json in the SageMaker model-metrics
format, plus the values the Condition step reads (ks, dppl).
"""

import argparse
import json
import os
import tarfile

import numpy as np
import pandas as pd
import xgboost as xgb
from scipy.stats import ks_2samp
from sklearn.metrics import brier_score_loss, roc_auc_score

from credit_features import FEATURE_ORDER, LABEL

BASE = "/opt/ml/processing"


def main(a) -> None:
    with tarfile.open(f"{BASE}/model/model.tar.gz") as t:
        t.extractall(f"{BASE}/model", filter="data")
    booster = xgb.Booster()
    booster.load_model(f"{BASE}/model/model.ubj")
    cal = json.load(open(f"{BASE}/model/calibrator.json"))
    features = json.load(open(f"{BASE}/model/metadata.json")).get("feature_order", FEATURE_ORDER)

    oot = pd.read_csv(f"{BASE}/oot/oot.csv")
    y = oot[LABEL].to_numpy()
    raw = booster.predict(xgb.DMatrix(oot[features].to_numpy(np.float32), feature_names=features, missing=np.nan))
    pd_ = np.interp(raw, cal["x"], cal["y"])

    auc = roc_auc_score(y, pd_)
    ks = ks_2samp(pd_[y == 1], pd_[y == 0]).statistic
    brier = brier_score_loss(y, pd_)

    # DPPL on facet "sex" (1 = male, 2 = female in source data) at the auto-approve threshold.
    approved = pd_ < a.approve_threshold
    rate = lambda m: float(approved[m].mean()) if m.any() else 0.0  # noqa: E731
    dppl = abs(rate(oot["sex"].to_numpy() == 1) - rate(oot["sex"].to_numpy() == 2))

    deciles = pd.qcut(pd_, 10, labels=False, duplicates="drop")
    decile_table = (
        pd.DataFrame({"decile": deciles, "y": y})
        .groupby("decile")["y"]
        .agg(["count", "mean"])
        .round(4)
        .reset_index()
        .to_dict(orient="records")
    )

    report = {
        "binary_classification_metrics": {
            "auc": {"value": round(auc, 4), "standard_deviation": "NaN"},
            "ks": {"value": round(ks, 4), "standard_deviation": "NaN"},
            "gini": {"value": round(2 * auc - 1, 4), "standard_deviation": "NaN"},
            "brier": {"value": round(brier, 4), "standard_deviation": "NaN"},
        },
        "fairness": {"dppl_sex": round(dppl, 4), "approve_threshold": a.approve_threshold},
        "decile_table": decile_table,
        "features": features,
        "oot_rows": int(len(oot)),
    }
    os.makedirs(f"{BASE}/evaluation", exist_ok=True)
    json.dump(report, open(f"{BASE}/evaluation/evaluation.json", "w"), indent=2)
    print(json.dumps(report["binary_classification_metrics"]), "dppl:", round(dppl, 4))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--approve-threshold", type=float, default=0.15)
    main(p.parse_args())
