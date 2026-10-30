"""Create a tiny but real model directory for the container contract test in CI.

Usage: python tests/make_tiny_model.py /tmp/model
"""

import json
import sys

import numpy as np
import xgboost as xgb

sys.path.insert(0, "src")
from credit_features import FEATURE_ORDER  # noqa: E402

out = sys.argv[1]
rng = np.random.default_rng(0)
X = rng.normal(size=(500, len(FEATURE_ORDER))).astype(np.float32)
y = (X[:, FEATURE_ORDER.index("max_delinquency")] + rng.normal(size=500) > 0.5).astype(int)
booster = xgb.train(
    {"objective": "binary:logistic", "max_depth": 2},
    xgb.DMatrix(X, label=y, feature_names=FEATURE_ORDER),
    num_boost_round=5,
)
booster.save_model(f"{out}/model.ubj")
json.dump({"x": [0.0, 1.0], "y": [0.0, 1.0]}, open(f"{out}/calibrator.json", "w"))
json.dump({"feature_order": FEATURE_ORDER}, open(f"{out}/metadata.json", "w"))
print("tiny model written to", out)
