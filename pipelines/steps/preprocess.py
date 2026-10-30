"""Processing step: curated data -> train / validation / OOT feature sets.

Outputs (all CSV, label first, no header, as XGBoost expects):
  /opt/ml/processing/train/train.csv
  /opt/ml/processing/validation/validation.csv
  /opt/ml/processing/oot/oot.csv            (features + label + facets, with header)
  /opt/ml/processing/baseline/baseline.jsonl (raw records as the endpoint receives them,
                                              used for the Model Monitor data-quality baseline)
  /opt/ml/processing/bias/bias.csv          (features + label + facets, for Clarify)
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

from credit_features import APPLICATION_FIELDS, FACET_COLUMNS, FEATURE_ORDER, HISTORY_FIELDS, LABEL, transform_frame

BASE = "/opt/ml/processing"


def psi(expected: pd.Series, actual: pd.Series, bins: int = 10) -> float:
    e, a_ = expected.dropna(), actual.dropna()
    if e.nunique() < 2 or a_.empty:
        return 0.0
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    ep = np.histogram(e, edges)[0] / len(e) + 1e-6
    ap = np.histogram(a_.clip(edges[0], edges[-1]), edges)[0] / len(a_) + 1e-6
    return float(np.sum((ap - ep) * np.log(ap / ep)))


def parse_months(spec: str) -> list[int]:
    lo, hi = (int(x) for x in spec.split("-"))
    return list(range(lo, hi + 1))


def main(a) -> None:
    if glob.glob(f"{BASE}/input/**/*.parquet", recursive=True):
        # Reading the directory keeps the hive partition column (application_month).
        df = pd.read_parquet(f"{BASE}/input")
        df["application_month"] = df["application_month"].astype(int)
    else:  # smoke runs use a small CSV fixture
        df = pd.concat(pd.read_csv(f) for f in glob.glob(f"{BASE}/input/**/*.csv", recursive=True))
    if a.sample_fraction < 1.0:
        df = df.sample(frac=a.sample_fraction, random_state=42)

    oot_months = [int(m) for m in a.oot_months.split(",")]
    train_months = parse_months(a.train_months)
    dev = df[df["application_month"].isin(train_months)]
    oot = df[df["application_month"].isin(oot_months)]

    rng = np.random.default_rng(42)
    is_val = rng.random(len(dev)) < 0.2
    tr, va = dev[~is_val], dev[is_val]

    # Optional time-decay sample weights (retrain candidate C3): newer months weigh more.
    def featurize(part: pd.DataFrame) -> pd.DataFrame:
        X = transform_frame(part)
        out = pd.concat([part[[LABEL]].reset_index(drop=True), X.reset_index(drop=True)], axis=1)
        return out

    for name, part in (("train", tr), ("validation", va)):
        os.makedirs(f"{BASE}/{name}", exist_ok=True)
        featurize(part).to_csv(f"{BASE}/{name}/{name}.csv", header=False, index=False)
    # Feature re-selection (retrain candidate C4): choose ONLY from the approved catalog
    # (FEATURE_ORDER) and drop features that are unstable across the training window.
    selected = FEATURE_ORDER
    if a.feature_set == "reselect":
        early = transform_frame(tr[tr["application_month"] <= np.median(train_months)])
        late = transform_frame(tr[tr["application_month"] > np.median(train_months)])
        stable = [f for f in FEATURE_ORDER if psi(early[f], late[f]) < a.psi_max]
        selected = stable if len(stable) >= 6 else FEATURE_ORDER
        print("Reselected features:", selected)
    with open(f"{BASE}/train/selected_features.json", "w") as f:
        json.dump(selected, f)

    if a.sample_weight_decay > 0:
        w = np.exp(-a.sample_weight_decay * (max(train_months) - tr["application_month"].to_numpy()))
        pd.Series(w).to_csv(f"{BASE}/train/train_weights.csv", header=False, index=False)

    os.makedirs(f"{BASE}/oot", exist_ok=True)
    oot_feat = pd.concat([featurize(oot), oot[FACET_COLUMNS].reset_index(drop=True)], axis=1)
    oot_feat.to_csv(f"{BASE}/oot/oot.csv", index=False)

    os.makedirs(f"{BASE}/bias", exist_ok=True)
    bias = pd.concat([featurize(tr), tr[FACET_COLUMNS].reset_index(drop=True)], axis=1)
    bias.to_csv(f"{BASE}/bias/bias.csv", index=False)

    os.makedirs(f"{BASE}/baseline", exist_ok=True)
    raw_cols = [c for c in APPLICATION_FIELDS if c != "customer_id"] + HISTORY_FIELDS
    with open(f"{BASE}/baseline/baseline.jsonl", "w") as f:
        for rec in tr[raw_cols].to_dict(orient="records"):
            f.write(json.dumps({k: float(v) for k, v in rec.items()}) + "\n")

    print(f"train={len(tr)} validation={len(va)} oot={len(oot)} features={len(FEATURE_ORDER)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sample-fraction", type=float, default=1.0)
    p.add_argument("--train-months", default="1-10")
    p.add_argument("--oot-months", default="11,12")
    p.add_argument("--sample-weight-decay", type=float, default=0.0)
    p.add_argument("--feature-set", choices=["champion", "reselect"], default="champion")
    p.add_argument("--psi-max", type=float, default=0.1)
    main(p.parse_args())
