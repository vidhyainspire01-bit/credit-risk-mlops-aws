"""EventBridge (model package Approved) -> GitHub repository_dispatch -> deploy.yml.

Only the central `credit-pd` group triggers deployment; dev-group approvals are ignored.
"""

import json
import os
import urllib.request

import boto3

REPO = os.environ["GITHUB_REPO"]  # owner/repo
SECRET_ID = os.environ["GITHUB_TOKEN_SECRET"]
DEPLOY_GROUP = os.environ.get("DEPLOY_GROUP", "credit-pd")

_token = None


def token() -> str:
    global _token
    if _token is None:
        _token = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"]
    return _token


def handler(event, _ctx):
    d = event["detail"]
    if d.get("ModelPackageGroupName") != DEPLOY_GROUP or d.get("ModelApprovalStatus") != "Approved":
        print("Ignored:", d.get("ModelPackageGroupName"), d.get("ModelApprovalStatus"))
        return {"dispatched": False}

    body = json.dumps(
        {
            "event_type": "model-approved",
            "client_payload": {
                "model_package_arn": d["ModelPackageArn"],
                "model_package_group": d["ModelPackageGroupName"],
                "version": d.get("ModelPackageVersion"),
            },
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/dispatches",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        print("Dispatched deploy for", d["ModelPackageArn"], "status", r.status)
    return {"dispatched": True, "model_package_arn": d["ModelPackageArn"]}
