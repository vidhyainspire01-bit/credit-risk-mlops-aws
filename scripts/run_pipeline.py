"""Upsert and start the training pipeline for an environment.

  python scripts/run_pipeline.py --env smoke            # local-mode DAG run, no registration (needs AWS credentials)
  python scripts/run_pipeline.py --env dev   --wait     # merge to develop
  python scripts/run_pipeline.py --env release --wait   # merge to main
  python scripts/run_pipeline.py --env release --candidate C3_time_decay   # used by retrain

Champion KS is read from the latest Approved version in the target group and passed
as a pipeline parameter, so the gate always compares against the real incumbent.
"""

import argparse
import json
import os
import sys

import boto3
import yaml

sys.path.insert(0, os.path.abspath("."))
from pipelines.credit_pd_pipeline import build  # noqa: E402


def champion_ks(sm, group: str) -> float:
    try:
        pkgs = sm.list_model_packages(
            ModelPackageGroupName=group,
            ModelApprovalStatus="Approved",
            SortBy="CreationTime",
            SortOrder="Descending",
            MaxResults=1,
        )
    except sm.exceptions.ClientError:
        return 0.0
    if not pkgs["ModelPackageSummaryList"]:
        return 0.0
    d = sm.describe_model_package(ModelPackageName=pkgs["ModelPackageSummaryList"][0]["ModelPackageArn"])
    uri = d["ModelMetrics"]["ModelQuality"]["Statistics"]["S3Uri"]
    bucket, key = uri.replace("s3://", "").split("/", 1)
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return float(json.loads(body)["binary_classification_metrics"]["ks"]["value"])


def main(a) -> None:
    cfg = yaml.safe_load(open("config/pipeline_params.yaml"))
    env_cfg, common = cfg["environments"][a.env], cfg["common"]
    role = os.environ["SAGEMAKER_ROLE_ARN"]
    image = os.environ["IMAGE_URI"]
    bucket = os.environ["ARTIFACT_BUCKET"]

    if env_cfg.get("local_mode"):
        from sagemaker.workflow.pipeline_context import LocalPipelineSession

        session = LocalPipelineSession()
    else:
        from sagemaker.workflow.pipeline_context import PipelineSession

        session = PipelineSession()

    group = env_cfg.get("model_package_group")
    pipe = build(
        env_name=a.env,
        env_cfg=env_cfg,
        common=common,
        role=role,
        image_uri=image,
        bucket=bucket,
        session=session,
        model_package_group=group,
    )
    pipe.upsert(role_arn=role, tags=[{"Key": "Project", "Value": "credit-pd-demo"}])

    run_params = {
        "GitSha": os.environ.get("GITHUB_SHA", "local"),
        "MlflowRunId": a.mlflow_run_id,
        "ChampionKS": champion_ks(boto3.client("sagemaker"), group) if group else 0.0,
    }
    if a.input_uri:
        run_params["InputDataUri"] = a.input_uri
    if a.candidate:
        c = cfg["retrain_candidates"][a.candidate]
        run_params.update(
            {
                "CandidateId": a.candidate,
                "TrainMonths": c["train_months"],
                "SampleWeightDecay": float(c["sample_weight_decay"]),
                "FeatureSet": c["feature_set"],
            }
        )

    execution = pipe.start(parameters=run_params)
    print("Started:", getattr(execution, "arn", "local execution"))
    if a.wait and not env_cfg.get("local_mode"):
        execution.wait(delay=60, max_attempts=90)
        steps = execution.list_steps()
        failed = [s for s in steps if s["StepStatus"] == "Failed"]
        for s in steps:
            print(f'{s["StepName"]:22s} {s["StepStatus"]}')
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env", choices=["smoke", "dev", "release"], required=True)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--mlflow-run-id", default="none")
    p.add_argument("--candidate", default=None)
    p.add_argument("--input-uri", default=None)
    main(p.parse_args())
