"""Feature definitions for the credit PD model.

Source data: UCI "Default of Credit Card Clients" (30,000 customers).
- Application fields arrive in the API request.
- History fields come from the online Feature Store (customer repayment history).
- SEX and MARRIAGE are never model inputs; they are kept only as Clarify facets.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import pandas as pd

LABEL = "default_next_month"

APPLICATION_FIELDS = ["customer_id", "credit_limit", "education", "age"]

PAY_STATUS = ["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"]
BILL_AMT = [f"bill_amt{i}" for i in range(1, 7)]
PAY_AMT = [f"pay_amt{i}" for i in range(1, 7)]
HISTORY_FIELDS = PAY_STATUS + BILL_AMT + PAY_AMT

FACET_COLUMNS = ["sex", "marriage"]

FEATURE_ORDER = [
    "credit_limit_log",
    "age",
    "education_grad",
    "education_univ",
    "max_delinquency",
    "months_delinquent",
    "recent_delinquency",
    "utilization",
    "utilization_trend",
    "payment_ratio",
    "avg_payment_log",
    "months_zero_payment",
]

# +1 = PD can only increase with the feature, -1 = only decrease, 0 = free.
MONOTONE_CONSTRAINTS = {
    "credit_limit_log": -1,
    "age": 0,
    "education_grad": 0,
    "education_univ": 0,
    "max_delinquency": 1,
    "months_delinquent": 1,
    "recent_delinquency": 1,
    "utilization": 1,
    "utilization_trend": 1,
    "payment_ratio": -1,
    "avg_payment_log": -1,
    "months_zero_payment": 1,
}

REASON_CODES = {
    "credit_limit_log": ("R01", "Low credit limit relative to portfolio"),
    "age": ("R02", "Applicant age profile"),
    "education_grad": ("R03", "Education profile"),
    "education_univ": ("R03", "Education profile"),
    "max_delinquency": ("R04", "Serious delinquency in repayment history"),
    "months_delinquent": ("R05", "Frequent late payments in the last 6 months"),
    "recent_delinquency": ("R06", "Late payment in the most recent month"),
    "utilization": ("R07", "High utilisation of existing credit limit"),
    "utilization_trend": ("R08", "Rising balance over the last 6 months"),
    "payment_ratio": ("R09", "Low repayment relative to the amount billed"),
    "avg_payment_log": ("R10", "Low average repayment amount"),
    "months_zero_payment": ("R11", "Months with no repayment made"),
}


def _num(v, default=np.nan) -> float:
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def transform(record: Mapping) -> dict:
    """Single record (application + history) -> model features. Used at inference."""
    limit = max(_num(record.get("credit_limit"), 0.0), 1.0)
    edu = int(_num(record.get("education"), 0))
    pay = [_num(record.get(c)) for c in PAY_STATUS]
    bill = [_num(record.get(c)) for c in BILL_AMT]
    paid = [_num(record.get(c)) for c in PAY_AMT]

    pay_valid = [p for p in pay if not math.isnan(p)]
    bill_sum = np.nansum([max(b, 0.0) for b in bill]) if bill else np.nan
    paid_sum = np.nansum(paid) if paid else np.nan

    return {
        "credit_limit_log": math.log1p(limit),
        "age": _num(record.get("age")),
        "education_grad": 1.0 if edu == 1 else 0.0,
        "education_univ": 1.0 if edu == 2 else 0.0,
        "max_delinquency": max(pay_valid) if pay_valid else np.nan,
        "months_delinquent": float(sum(p > 0 for p in pay_valid)) if pay_valid else np.nan,
        "recent_delinquency": 1.0 if (not math.isnan(pay[0]) and pay[0] > 0) else 0.0,
        "utilization": (bill[0] / limit) if not math.isnan(bill[0]) else np.nan,
        "utilization_trend": (
            ((bill[0] - bill[5]) / limit) if not (math.isnan(bill[0]) or math.isnan(bill[5])) else np.nan
        ),
        "payment_ratio": min(paid_sum / bill_sum, 5.0) if bill_sum and bill_sum > 0 else 1.0,
        "avg_payment_log": math.log1p(max(paid_sum, 0.0) / 6.0) if not math.isnan(paid_sum) else np.nan,
        "months_zero_payment": float(sum((p == 0) for p in paid if not math.isnan(p))),
    }


def transform_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Batch version used by the training pipeline. Same logic, row by row, for exact parity."""
    rows = [transform(r) for r in df.to_dict(orient="records")]
    return pd.DataFrame(rows, columns=FEATURE_ORDER, index=df.index)
