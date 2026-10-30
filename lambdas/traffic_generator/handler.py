"""Simulated loan origination traffic (EventBridge Scheduler, every 5 minutes).

- Samples customers from the OOT file and calls the scoring API through the same path a
  real caller uses (SigV4-signed HTTP API call).
- Writes "matured" ground truth for scored applications so the model-quality monitor runs
  within a day (in production, labels arrive months later from loan performance data).
- DRIFT_MODE=on (set by `make drift-on`) shifts the population: higher utilisation and
  more delinquency, to trigger feature and score drift alarms.
"""

import csv
import io
import json
import os
import random
import urllib.request
import uuid
from datetime import UTC, datetime

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

API_URL = os.environ["API_URL"]
BUCKET = os.environ["ARTIFACT_BUCKET"]
SAMPLE_KEY = os.environ.get("SAMPLE_KEY", "traffic/sample_customers.csv")
GT_PREFIX = os.environ.get("GT_PREFIX", "groundtruth")
REQUESTS = int(os.environ.get("REQUESTS_PER_RUN", "40"))
ssm, s3 = boto3.client("ssm"), boto3.client("s3")
_rows = None


def rows():
    global _rows
    if _rows is None:
        body = s3.get_object(Bucket=BUCKET, Key=SAMPLE_KEY)["Body"].read().decode()
        _rows = list(csv.DictReader(io.StringIO(body)))
    return _rows


def call(payload: dict) -> dict:
    session = boto3.Session()
    req = AWSRequest(method="POST", url=API_URL, data=json.dumps(payload), headers={"Content-Type": "application/json"})
    SigV4Auth(session.get_credentials(), "execute-api", session.region_name).add_auth(req)
    r = urllib.request.Request(API_URL, data=req.body, headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(r, timeout=5) as resp:
        return json.loads(resp.read())


def handler(_event, _ctx):
    drift = ssm.get_parameter(Name="/credit-pd/drift-mode")["Parameter"]["Value"] == "on"
    now = datetime.now(UTC)
    gt_lines = []
    for r in random.sample(rows(), REQUESTS):
        app_id = f"APP-{uuid.uuid4().hex[:12]}"
        payload = {
            "application_id": app_id,
            "customer_id": r["customer_id"],
            "credit_limit": float(r["credit_limit"]),
            "education": int(r["education"]),
            "age": float(r["age"]),
        }
        if drift:  # tighter limits for the same customers -> utilisation jumps
            payload["credit_limit"] = round(payload["credit_limit"] * 0.4, 0)
        try:
            res = call(payload)
        except Exception as e:  # keep generating; the API's own metrics capture failures
            print("call failed:", e)
            continue
        if res.get("pd") is not None:
            label = int(r["default_next_month"])
            if drift and random.random() < 0.15:
                label = 1  # concept shift: more defaults than the model expects
            gt_lines.append(
                json.dumps(
                    {
                        "groundTruthData": {"data": str(label), "encoding": "CSV"},
                        "eventMetadata": {"eventId": app_id},
                        "eventVersion": "0",
                    }
                )
            )
    if gt_lines:
        key = f"{GT_PREFIX}/{now:%Y/%m/%d/%H}/{uuid.uuid4().hex}.jsonl"
        s3.put_object(Bucket=BUCKET, Key=key, Body="\n".join(gt_lines).encode())
    return {"sent": REQUESTS, "labels": len(gt_lines), "drift": drift}
