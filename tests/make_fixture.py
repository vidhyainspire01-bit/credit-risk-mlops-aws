"""Generate tests/fixtures/credit_sample.csv: synthetic rows with the curated schema.

Used only by PR smoke training (local mode). No real data in the repo.
"""

import os

import numpy as np
import pandas as pd

rng = np.random.default_rng(1)
n = 1200
pay = rng.choice([-2, -1, 0, 1, 2, 3], size=(n, 6), p=[0.1, 0.2, 0.5, 0.1, 0.07, 0.03])
limit = rng.choice([20000, 50000, 100000, 200000, 360000], size=n)
bill = (limit[:, None] * rng.uniform(0, 1.0, size=(n, 6))).round()
paid = (bill * rng.uniform(0, 0.3, size=(n, 6))).round()
logit = -2.0 + 0.8 * pay.max(axis=1) + 1.2 * (bill[:, 0] / limit) - 3 * (paid.sum(1) / (bill.sum(1) + 1))
y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)

df = pd.DataFrame(
    {
        "customer_id": [str(i) for i in range(1, n + 1)],
        "credit_limit": limit,
        "sex": rng.choice([1, 2], n),
        "education": rng.choice([1, 2, 3], n),
        "marriage": rng.choice([1, 2], n),
        "age": rng.integers(21, 65, n),
        **{c: pay[:, i] for i, c in enumerate(["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"])},
        **{f"bill_amt{i + 1}": bill[:, i] for i in range(6)},
        **{f"pay_amt{i + 1}": paid[:, i] for i in range(6)},
        "default_next_month": y,
        "application_month": rng.integers(1, 13, n),
        "event_time": "2026-10-01T00:00:00Z",
    }
)
os.makedirs("tests/fixtures", exist_ok=True)
df.to_csv("tests/fixtures/credit_sample.csv", index=False)
print("rows", n, "default rate", round(y.mean(), 3))
