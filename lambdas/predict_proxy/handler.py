"""Scoring API orchestrator: API Gateway -> this Lambda -> SageMaker endpoint -> caller.

1. validate request
2. fetch customer history from the online Feature Store
3. knockout (policy) rules, no model call
4. invoke endpoint with InferenceId = application_id (joins ground truth later)
5. decision band from SSM thresholds (credit policy lives outside the model)
6. decision log to S3; fallback to MANUAL_REVIEW on any model-path failure
"""

import json
import os
import time
import uuid
from datetime import UTC, datetime

import boto3
from botocore.config import Config

ENDPOINT = os.environ["ENDPOINT_NAME"]
FEATURE_GROUP = os.environ.get("FEATURE_GROUP", "credit-customer-history")
LOG_BUCKET = os.environ["DECISION_LOG_BUCKET"]
THRESHOLD_PARAM = os.environ.get("THRESHOLD_PARAM", "/credit-pd/thresholds")

_cfg = Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 1})
runtime = boto3.client("sagemaker-runtime", config=_cfg)
fs = boto3.client("sagemaker-featurestore-runtime", config=_cfg)
s3 = boto3.client("s3")
cw = boto3.client("cloudwatch")
ssm = boto3.client("ssm")

REQUIRED = {"application_id": str, "customer_id": str, "credit_limit": (int, float), "age": (int, float)}
_thresholds, _thresholds_at = None, 0.0


def thresholds() -> dict:
    global _thresholds, _thresholds_at
    if _thresholds is None or time.time() - _thresholds_at > 300:
        _thresholds = json.loads(ssm.get_parameter(Name=THRESHOLD_PARAM)["Parameter"]["Value"])
        _thresholds_at = time.time()
    return _thresholds


def respond(code: int, body: dict) -> dict:
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def metric(name: str, value: float = 1.0) -> None:
    cw.put_metric_data(Namespace="CreditPD/API", MetricData=[{"MetricName": name, "Value": value}])


def get_history(customer_id: str) -> dict:
    rec = fs.get_record(FeatureGroupName=FEATURE_GROUP, RecordIdentifierValueAsString=customer_id)
    return {f["FeatureName"]: f["ValueAsString"] for f in rec.get("Record", [])}


def band(pd_: float, t: dict) -> str:
    if pd_ < t["approve_below"]:
        return "APPROVE"
    if pd_ < t["decline_from"]:
        return "MANUAL_REVIEW"
    return "DECLINE"


def handler(event, _ctx):
    started = time.time()
    try:
        req = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "invalid JSON"})
    for k, typ in REQUIRED.items():
        if not isinstance(req.get(k), typ):
            return respond(400, {"error": f"missing or invalid field: {k}"})

    app_id, t = req["application_id"], thresholds()
    result = {"application_id": app_id, "request_id": str(uuid.uuid4()), "fallback": False}

    # Knockout rules: policy, not model
    if req["age"] < t["min_age"] or req["credit_limit"] > t["max_limit"]:
        result.update(
            decision="DECLINE",
            reason_codes=[{"code": "K01", "text": "Outside product policy"}],
            pd=None,
            model_version=None,
        )
        return finish(result, req, started)

    try:
        history = get_history(req["customer_id"])
        payload = {**{k: v for k, v in req.items() if k != "application_id"}, **history}
        resp = runtime.invoke_endpoint(
            EndpointName=ENDPOINT, ContentType="application/json", Body=json.dumps(payload), InferenceId=app_id
        )
        pred = json.loads(resp["Body"].read())
        result.update(
            pd=pred["pd"],
            decision=band(pred["pd"], t),
            reason_codes=pred["reason_codes"],
            model_version=pred["model_version"],
            variant=resp["ResponseMetadata"]["HTTPHeaders"].get("x-amzn-invoked-production-variant"),
            history_found=bool(history),
        )
    except Exception as e:  # any model-path failure -> safe fallback, never silent approve
        metric("Fallback")
        result.update(
            decision="MANUAL_REVIEW",
            pd=None,
            reason_codes=[],
            model_version=None,
            fallback=True,
            fallback_reason=type(e).__name__,
        )
    return finish(result, req, started)


def finish(result: dict, req: dict, started: float) -> dict:
    result["latency_ms"] = round((time.time() - started) * 1000, 1)
    metric("Latency", result["latency_ms"])
    metric(f"Decision_{result['decision']}")
    now = datetime.now(UTC)
    key = f"decisions/dt={now:%Y-%m-%d}/hour={now:%H}/{result['request_id']}.json"
    s3.put_object(
        Bucket=LOG_BUCKET,
        Key=key,
        Body=json.dumps({**result, "ts": now.isoformat(), "customer_id": req["customer_id"]}),
    )
    return respond(200, result)
