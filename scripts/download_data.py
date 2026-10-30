"""Phase 1: land the source dataset into the S3 raw zone.

Simulates the core-banking extract. Adds:
- customer_id (string key used by Feature Store)
- application_month (synthetic 1..12) so an out-of-time split is possible
- event_time (ISO timestamp required by Feature Store)

Usage: python scripts/download_data.py --bucket <raw-bucket>
"""

import argparse
import io
import zipfile
from datetime import UTC, datetime

import boto3
import numpy as np
import pandas as pd
import requests

URL = "https://archive.ics.uci.edu/static/public/350/default+of+credit+card+clients.zip"

RENAME = {
    "ID": "customer_id",
    "LIMIT_BAL": "credit_limit",
    "SEX": "sex",
    "EDUCATION": "education",
    "MARRIAGE": "marriage",
    "AGE": "age",
    "PAY_0": "pay_0",
    **{f"PAY_{i}": f"pay_{i}" for i in range(2, 7)},
    **{f"BILL_AMT{i}": f"bill_amt{i}" for i in range(1, 7)},
    **{f"PAY_AMT{i}": f"pay_amt{i}" for i in range(1, 7)},
    "default payment next month": "default_next_month",
}


def main(bucket: str, prefix: str) -> None:
    resp = requests.get(URL, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        name = next(n for n in z.namelist() if n.endswith(".xls"))
        df = pd.read_excel(z.open(name), header=1)

    df = df.rename(columns=RENAME)
    df["customer_id"] = df["customer_id"].astype(str)
    rng = np.random.default_rng(42)
    df["application_month"] = rng.integers(1, 13, size=len(df))
    df["event_time"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    ingest_date = datetime.now(UTC).strftime("%Y-%m-%d")
    key = f"{prefix}/source=core_banking/ingest_date={ingest_date}/credit_clients.csv"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=df.to_csv(index=False).encode())
    print(f"Landed {len(df)} rows to s3://{bucket}/{key}")
    print(f"Default rate: {df['default_next_month'].mean():.3f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="raw")
    a = p.parse_args()
    main(a.bucket, a.prefix)
