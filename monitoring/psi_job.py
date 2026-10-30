"""Fallback drift monitor: PSI per input field and on the PD score, from endpoint data capture.

Run as a scheduled SageMaker Processing job (or locally for debugging):
  python monitoring/psi_job.py --capture s3://.../datacapture/credit-pd-prod/ --baseline s3://.../baseline.jsonl

Publishes CloudWatch metrics in namespace CreditPD/Drift:
  FeaturePSI (dimension Feature=<name>), MaxFeaturePSI, ScorePSI
which back the alarms credit-pd-feature-drift and credit-pd-score-drift.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta

import boto3
import numpy as np
import pandas as pd

s3, cw = boto3.client("s3"), boto3.client("cloudwatch")


def psi(expected: pd.Series, actual: pd.Series, bins: int = 10) -> float:
    e, a = expected.dropna(), actual.dropna()
    if e.nunique() < 2 or a.empty:
        return 0.0
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    ep = np.histogram(e, edges)[0] / len(e) + 1e-6
    ap = np.histogram(a.clip(edges[0], edges[-1]), edges)[0] / len(a) + 1e-6
    return float(np.sum((ap - ep) * np.log(ap / ep)))


def read_capture(uri: str, hours: int) -> tuple[pd.DataFrame, pd.Series]:
    bucket, prefix = uri.replace("s3://", "").split("/", 1)
    since = datetime.now(UTC) - timedelta(hours=hours)
    inputs, scores = [], []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            if o["LastModified"] < since:
                continue
            for line in s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode().splitlines():
                rec = json.loads(line)["captureData"]
                inputs.append(json.loads(rec["endpointInput"]["data"]))
                scores.append(json.loads(rec["endpointOutput"]["data"])["pd"])
    return pd.DataFrame(inputs), pd.Series(scores)


def main(a):
    b, k = a.baseline.replace("s3://", "").split("/", 1)
    base = pd.read_json(s3.get_object(Bucket=b, Key=k)["Body"], lines=True)
    live, scores = read_capture(a.capture, a.hours)
    if live.empty:
        print("No capture data in window")
        return
    feats = {c: psi(base[c], pd.to_numeric(live[c], errors="coerce")) for c in base.columns if c in live}
    data = [
        {"MetricName": "FeaturePSI", "Value": v, "Dimensions": [{"Name": "Feature", "Value": c}]}
        for c, v in feats.items()
    ]
    data.append({"MetricName": "MaxFeaturePSI", "Value": max(feats.values())})
    if a.baseline_scores:
        bb, bk = a.baseline_scores.replace("s3://", "").split("/", 1)
        ref = pd.read_csv(s3.get_object(Bucket=bb, Key=bk)["Body"])["pd"]
        data.append({"MetricName": "ScorePSI", "Value": psi(ref, scores)})
    for i in range(0, len(data), 20):
        cw.put_metric_data(Namespace="CreditPD/Drift", MetricData=data[i : i + 20])
    print(json.dumps({k: round(v, 3) for k, v in sorted(feats.items(), key=lambda x: -x[1])[:5]}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--capture", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--baseline-scores", default=None, help="CSV with a 'pd' column from the champion's first day")
    p.add_argument("--hours", type=int, default=6)
    main(p.parse_args())
