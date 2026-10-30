"""Preflight: can this account and region run the POC? Read-only, creates nothing, costs nothing.

  python scripts/preflight.py                # ap-south-1
  python scripts/preflight.py --region us-east-1

Checks:
  1. AWS identity
  2. Each service API answers in the region (SageMaker, MLflow, Feature Store, Glue, Step Functions, ...)
  3. SageMaker quotas the POC needs (endpoint, training, processing, spot)
Prints the exact quota names to request where the value is too low.
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

# Quota name (as shown in Service Quotas) -> minimum the POC needs
NEEDED = {
    "ml.m5.large for endpoint usage": 4,  # staging + prod champion + challenger/shadow
    "ml.m5.large for processing job usage": 4,  # pipeline steps, Model Monitor, Clarify, parallel retrain
    "ml.m5.large for training job usage": 4,  # up to 4 retrain candidates in parallel
    "ml.m5.large for spot training job usage": 4,
    "Studio JupyterLab Apps running on ml.m5.large instances": 1,
}

API_CHECKS = {
    "SageMaker": ("sagemaker", lambda c: c.list_endpoints(MaxResults=1)),
    "SageMaker Pipelines": ("sagemaker", lambda c: c.list_pipelines(MaxResults=1)),
    "Model Registry": ("sagemaker", lambda c: c.list_model_package_groups(MaxResults=1)),
    "Feature Store": ("sagemaker", lambda c: c.list_feature_groups(MaxResults=1)),
    "Model Monitor": ("sagemaker", lambda c: c.list_monitoring_schedules(MaxResults=1)),
    "Managed MLflow": ("sagemaker", lambda c: c.list_mlflow_tracking_servers(MaxResults=1)),
    "Shadow testing (inference experiments)": ("sagemaker", lambda c: c.list_inference_experiments(MaxResults=1)),
    "Glue (+ Data Quality)": ("glue", lambda c: c.get_jobs(MaxResults=1)),
    "Step Functions": ("stepfunctions", lambda c: c.list_state_machines(maxResults=1)),
    "EventBridge Scheduler": ("scheduler", lambda c: c.list_schedules(MaxResults=1)),
    "API Gateway (HTTP)": ("apigatewayv2", lambda c: c.get_apis(MaxResults="1")),
    "ECR": ("ecr", lambda c: c.describe_repositories(maxResults=1)),
}


def main(region: str) -> int:
    problems = 0
    session = boto3.Session(region_name=region)
    ident = session.client("sts").get_caller_identity()
    print(f"Account {ident['Account']}  as {ident['Arn']}  region {region}\n")

    print("Service availability")
    for name, (svc, call) in API_CHECKS.items():
        try:
            call(session.client(svc))
            print(f"  OK    {name}")
        except (EndpointConnectionError, ClientError, AttributeError) as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            if code in ("AccessDenied", "AccessDeniedException"):
                print(f"  ?     {name}: available, but your user lacks permission ({code})")
            else:
                problems += 1
                print(f"  FAIL  {name}: {code}")

    print("\nSageMaker quotas")
    sq = session.client("service-quotas")
    found = {}
    for page in sq.get_paginator("list_service_quotas").paginate(ServiceCode="sagemaker"):
        for q in page["Quotas"]:
            if q["QuotaName"] in NEEDED:
                found[q["QuotaName"]] = (q["Value"], q["QuotaCode"])
    for qname, need in NEEDED.items():
        value, code = found.get(qname, (0, "unknown"))
        ok = value >= need
        problems += 0 if ok else 1
        print(
            f"  {'OK  ' if ok else 'LOW '}  {qname}: {value:g} (need {need})"
            + ("" if ok else f"  -> request quota code {code}")
        )

    print("\nResult:", "ready" if problems == 0 else f"{problems} item(s) to fix before `make up`")
    if problems:
        print("Request increases: Service Quotas console > AWS services > Amazon SageMaker > search the names above,")
        print(
            "or: aws service-quotas request-service-quota-increase --service-code sagemaker "
            f"--quota-code <code> --desired-value <n> --region {region}"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--region", default="ap-south-1")
    sys.exit(main(p.parse_args().region))
