"""Model logic inside the endpoint container.

model_fn  : runs ONCE per container start (loads model, calibrator, feature list into memory)
input_fn  : per request, parse JSON + preprocess with the shared credit_features library
predict_fn: per request, score + calibrate + SHAP reason codes (exact TreeSHAP via pred_contribs)
output_fn : per request, serialise with model version for provenance
"""

import json
import os

import numpy as np
import xgboost as xgb

from credit_features import FEATURE_ORDER, REASON_CODES, transform


def model_fn(model_dir: str) -> dict:
    booster = xgb.Booster()
    booster.load_model(os.path.join(model_dir, "model.ubj"))
    with open(os.path.join(model_dir, "calibrator.json")) as f:
        cal = json.load(f)
    meta_path = os.path.join(model_dir, "metadata.json")
    features = (
        json.load(open(meta_path)).get("feature_order", FEATURE_ORDER) if os.path.exists(meta_path) else FEATURE_ORDER
    )
    return {"booster": booster, "cal": cal, "features": features}


def input_fn(body, content_type: str = "application/json") -> dict:
    if not content_type.startswith("application/json"):
        raise ValueError(f"Unsupported content type: {content_type}")
    return transform(json.loads(body))


def predict_fn(feats: dict, m: dict) -> dict:
    names = m["features"]
    x = np.array([[feats[f] for f in names]], dtype=np.float32)
    dmat = xgb.DMatrix(x, feature_names=names, missing=np.nan)
    raw = float(m["booster"].predict(dmat)[0])
    pd_ = float(np.interp(raw, m["cal"]["x"], m["cal"]["y"]))
    contribs = m["booster"].predict(dmat, pred_contribs=True)[0][:-1]  # last column is bias term
    reasons, seen = [], set()
    for i in np.argsort(contribs)[::-1]:
        if contribs[i] <= 0 or len(reasons) == 5:
            break
        code, text = REASON_CODES[names[i]]
        if code not in seen:
            seen.add(code)
            reasons.append({"code": code, "text": text, "impact": round(float(contribs[i]), 4)})
    return {"pd": round(pd_, 6), "raw_score": round(raw, 6), "reason_codes": reasons}


def output_fn(pred: dict, accept: str = "application/json") -> str:
    return json.dumps({**pred, "model_version": os.environ.get("MODEL_VERSION", "unknown")})
