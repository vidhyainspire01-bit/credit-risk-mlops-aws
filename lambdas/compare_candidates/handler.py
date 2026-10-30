"""After parallel candidate pipelines finish: pick the best and leave ONE pending version.

Candidates register into `credit-pd` as PendingManualApproval (gated by the pipeline's
condition step). This function keeps the best one by OOT KS, rejects the others with a
reason, and writes a drift evidence report next to the winner for Model Risk.
"""

import json
import os
from datetime import UTC, datetime

import boto3

GROUP = os.environ.get("MODEL_PACKAGE_GROUP", "credit-pd")
BUCKET = os.environ["ARTIFACT_BUCKET"]
sm, s3 = boto3.client("sagemaker"), boto3.client("s3")


def metrics(arn: str) -> dict:
    d = sm.describe_model_package(ModelPackageName=arn)
    uri = d["ModelMetrics"]["ModelQuality"]["Statistics"]["S3Uri"]
    b, k = uri.replace("s3://", "").split("/", 1)
    m = json.loads(s3.get_object(Bucket=b, Key=k)["Body"].read())
    return {
        "arn": arn,
        "candidate": d.get("CustomerMetadataProperties", {}).get("candidate_id"),
        "ks": m["binary_classification_metrics"]["ks"]["value"],
        "auc": m["binary_classification_metrics"]["auc"]["value"],
        "dppl": m["fairness"]["dppl_sex"],
        "created": d["CreationTime"],
    }


def handler(event, _ctx):
    started = event["started_at"]
    pkgs = sm.list_model_packages(
        ModelPackageGroupName=GROUP,
        ModelApprovalStatus="PendingManualApproval",
        SortBy="CreationTime",
        SortOrder="Descending",
        MaxResults=20,
    )
    cands = [
        metrics(p["ModelPackageArn"])
        for p in pkgs["ModelPackageSummaryList"]
        if p["CreationTime"].isoformat() >= started
    ]
    if not cands:
        return {"winner": None, "message": "No candidate passed the pipeline quality gate."}

    cands.sort(key=lambda c: (c["ks"], -c["dppl"]), reverse=True)
    winner, losers = cands[0], cands[1:]
    for c in losers:
        sm.update_model_package(
            ModelPackageArn=c["arn"],
            ModelApprovalStatus="Rejected",
            ApprovalDescription=f"Retrain candidate not selected: KS {c['ks']} < {winner['ks']}",
        )

    report = {
        "drift": event.get("triage"),
        "selected": winner,
        "rejected": losers,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    key = f"evidence/retrain/{winner['arn'].split('/')[-1]}.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(report, default=str, indent=2))
    sm.update_model_package(
        ModelPackageArn=winner["arn"], CustomerMetadataProperties={"drift_evidence": f"s3://{BUCKET}/{key}"}
    )
    return {"winner": winner["arn"], "ks": winner["ks"], "evidence": f"s3://{BUCKET}/{key}"}
