"""First state of the retrain Step Function: classify the drift and choose a response.

Input: CloudWatch alarm state-change event (via EventBridge) or a manual {"force": "<type>"}.
Output: {"action": "notify_only" | "retrain", "drift_type": ..., "candidates": [ {pipeline params} ]}

Rules:
- data quality violations  -> notify Data Engineering, NEVER retrain on broken data
- cooldown: no second automated retrain inside COOLDOWN_HOURS
- covariate drift          -> refresh + time decay
- population drift         -> refresh + recent window + time decay
- concept drift            -> all of the above + feature re-selection from the approved catalog
"""

import os
from datetime import UTC, datetime, timedelta

import boto3

PIPELINE = os.environ.get("PIPELINE_NAME", "credit-pd-training-release")
COOLDOWN_HOURS = int(os.environ.get("COOLDOWN_HOURS", "6"))  # 30 days in production

sm = boto3.client("sagemaker")

ALARM_TO_TYPE = {
    "credit-pd-data-quality-violations": "data_quality",
    "credit-pd-feature-drift": "covariate",
    "credit-pd-score-drift": "population",
    "credit-pd-model-quality": "concept",
}

# Mirrors config/pipeline_params.yaml -> retrain_candidates
STRATEGIES = {
    "C1_refresh": {"TrainMonths": "1-10", "SampleWeightDecay": "0.0", "FeatureSet": "champion"},
    "C2_recent": {"TrainMonths": "5-10", "SampleWeightDecay": "0.0", "FeatureSet": "champion"},
    "C3_time_decay": {"TrainMonths": "1-10", "SampleWeightDecay": "0.15", "FeatureSet": "champion"},
    "C4_reselect": {"TrainMonths": "1-10", "SampleWeightDecay": "0.0", "FeatureSet": "reselect"},
}
BY_TYPE = {
    "covariate": ["C1_refresh", "C3_time_decay"],
    "population": ["C1_refresh", "C2_recent", "C3_time_decay"],
    "concept": ["C1_refresh", "C2_recent", "C3_time_decay", "C4_reselect"],
}


def recent_retrain() -> bool:
    since = datetime.now(UTC) - timedelta(hours=COOLDOWN_HOURS)
    runs = sm.list_pipeline_executions(PipelineName=PIPELINE, CreatedAfter=since, MaxResults=50)
    return any(
        e.get("PipelineExecutionDisplayName", "").startswith("retrain-") for e in runs["PipelineExecutionSummaries"]
    )


def handler(event, _ctx):
    detail = event.get("detail", {})
    drift_type = event.get("force") or ALARM_TO_TYPE.get(detail.get("alarmName", ""), "unknown")
    now = datetime.now(UTC)
    out = {"drift_type": drift_type, "started_at": now.isoformat(), "alarm": detail.get("alarmName")}

    if drift_type in ("data_quality", "unknown"):
        return {
            **out,
            "action": "notify_only",
            "message": "Data quality issue: routed to Data Engineering. No retrain on broken data.",
        }
    if not event.get("force") and recent_retrain():
        return {**out, "action": "notify_only", "message": f"Cooldown active ({COOLDOWN_HOURS}h). Retrain skipped."}

    candidates = [
        {
            "display_name": f"retrain-{c.lower().replace('_', '-')}-{now:%m%d%H%M}",
            "params": [
                {"Name": "CandidateId", "Value": c},
                {"Name": "ApprovalStatus", "Value": "PendingManualApproval"},
            ]
            + [{"Name": k, "Value": v} for k, v in STRATEGIES[c].items()],
        }
        for c in BY_TYPE[drift_type]
    ]
    return {**out, "action": "retrain", "candidates": candidates}
