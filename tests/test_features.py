import math

import pandas as pd

from credit_features import FEATURE_ORDER, transform, transform_frame

BASE = {
    "customer_id": "1",
    "credit_limit": 50000,
    "education": 2,
    "age": 35,
    **{c: 0 for c in ["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"]},
    **{f"bill_amt{i}": 10000 for i in range(1, 7)},
    **{f"pay_amt{i}": 2000 for i in range(1, 7)},
}


def test_all_features_present():
    out = transform(BASE)
    assert list(out) == FEATURE_ORDER


def test_delinquency_increases_risk_features():
    late = {**BASE, "pay_0": 3}
    out = transform(late)
    assert out["max_delinquency"] == 3
    assert out["recent_delinquency"] == 1.0
    assert out["months_delinquent"] == 1.0


def test_missing_history_gives_nan_not_default():
    rec = {k: v for k, v in BASE.items() if not k.startswith(("pay_", "bill_"))}
    out = transform(rec)
    assert math.isnan(out["max_delinquency"])
    assert math.isnan(out["utilization"])


def test_sensitive_attributes_not_features():
    assert "sex" not in FEATURE_ORDER and "marriage" not in FEATURE_ORDER


def test_frame_matches_single_record():
    df = pd.DataFrame([BASE, {**BASE, "pay_0": 2}])
    frame = transform_frame(df)
    for i, rec in enumerate(df.to_dict(orient="records")):
        single = transform(rec)
        for f in FEATURE_ORDER:
            assert frame.iloc[i][f] == single[f] or (math.isnan(frame.iloc[i][f]) and math.isnan(single[f]))
