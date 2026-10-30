"""Build tests/golden.jsonl (endpoint-shaped records) and the traffic sample from curated data.

Golden set: 100 fixed OOT records used for parity and contract tests.
Traffic sample: OOT customers with labels, uploaded for the traffic generator.
"""

import argparse
import json

import boto3
import pandas as pd

from credit_features import APPLICATION_FIELDS, HISTORY_FIELDS


def main(a):
    df = pd.read_parquet(a.curated)
    oot = df[df["application_month"].astype(int).isin([11, 12])]
    cols = [c for c in APPLICATION_FIELDS if c != "customer_id"] + HISTORY_FIELDS
    with open("tests/golden.jsonl", "w") as f:
        for rec in oot.sample(100, random_state=7)[cols].to_dict(orient="records"):
            f.write(json.dumps({k: float(v) for k, v in rec.items()}) + "\n")

    sample = oot[["customer_id", "credit_limit", "education", "age", "default_next_month"]]
    boto3.client("s3").put_object(
        Bucket=a.bucket, Key="traffic/sample_customers.csv", Body=sample.to_csv(index=False).encode()
    )
    print(f"golden=100 traffic_sample={len(sample)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--curated", required=True)
    p.add_argument("--bucket", required=True)
    main(p.parse_args())
