"""Morning summary (EventBridge Scheduler 09:00 IST) -> SNS email.

Reads the previous 24h of decision logs from S3 and endpoint metrics from CloudWatch.
In production, run the aggregation as Athena queries over the decision log table.
"""

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta

import boto3

BUCKET = os.environ["DECISION_LOG_BUCKET"]
TOPIC = os.environ["SNS_TOPIC_ARN"]
ENDPOINT = os.environ["ENDPOINT_NAME"]
s3, cw, sns = boto3.client("s3"), boto3.client("cloudwatch"), boto3.client("sns")


def decisions(day: datetime) -> list[dict]:
    out, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": f"decisions/dt={day:%Y-%m-%d}/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            out.append(json.loads(s3.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()))
        if not page.get("IsTruncated"):
            return out
        token = page["NextContinuationToken"]


def stat(metric: str, s: str, ext: bool = False) -> float:
    end = datetime.now(UTC)
    kw = {"ExtendedStatistics": [s]} if ext else {"Statistics": [s]}
    r = cw.get_metric_statistics(
        Namespace="AWS/SageMaker",
        MetricName=metric,
        StartTime=end - timedelta(days=1),
        EndTime=end,
        Period=86400,
        Dimensions=[{"Name": "EndpointName", "Value": ENDPOINT}, {"Name": "VariantName", "Value": "champion"}],
        **kw,
    )
    dp = r["Datapoints"]
    return (dp[0]["ExtendedStatistics"][s] if ext else dp[0][s]) if dp else 0.0


def handler(_event, _ctx):
    now = datetime.now(UTC)
    rows = decisions(now) + decisions(now - timedelta(days=1))
    n = len(rows) or 1
    dec = Counter(r["decision"] for r in rows)
    fallback = sum(r.get("fallback", False) for r in rows)
    pds = [r["pd"] for r in rows if r.get("pd") is not None]
    lat = sorted(r["latency_ms"] for r in rows)
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0

    def flag(ok: bool) -> str:
        return "OK " if ok else "ALERT"

    lines = [
        f"Credit PD daily report  {now:%Y-%m-%d}",
        f"{flag(True)}  Decisions: {len(rows)}",
        f"{flag(abs(dec['APPROVE'] / n - 0.6) < 0.1)}  Approval rate: {dec['APPROVE'] / n:.1%} "
        f"(review {dec['MANUAL_REVIEW'] / n:.1%}, decline {dec['DECLINE'] / n:.1%})",
        f"{flag(fallback / n < 0.01)}  Fallbacks: {fallback} ({fallback / n:.2%})",
        f"{flag(p95 < 300)}  API p95 latency: {p95:.0f} ms",
        f"{flag(True)}  Mean PD: {sum(pds) / len(pds):.3f}" if pds else "ALERT  No model scores",
        f"{flag(stat('Invocation5XXErrors', 'Sum') == 0)}  Endpoint 5xx (24h): {stat('Invocation5XXErrors', 'Sum'):.0f}",
        f"{flag(True)}  Endpoint p99 model latency: {stat('ModelLatency', 'p99', ext=True) / 1000:.0f} ms",
    ]
    sns.publish(TopicArn=TOPIC, Subject=f"Credit PD daily report {now:%Y-%m-%d}", Message="\n".join(lines))
    return {"lines": lines}
