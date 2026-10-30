"""Role-play Model Risk approval of the latest pending version.

  python scripts/approve.py --group credit-pd --note "Validated by MRM: KS 0.42, DPPL 0.03"

In the real setup this runs under the Model Risk IAM role. The data-science and CI roles
are denied sagemaker:UpdateModelPackage with ModelApprovalStatus=Approved (see Terraform),
so approval is a separate identity recorded in CloudTrail.
"""

import argparse

import boto3


def main(a):
    sm = boto3.client("sagemaker")
    pkgs = sm.list_model_packages(
        ModelPackageGroupName=a.group,
        ModelApprovalStatus="PendingManualApproval",
        SortBy="CreationTime",
        SortOrder="Descending",
        MaxResults=1,
    )["ModelPackageSummaryList"]
    if not pkgs:
        raise SystemExit(f"No pending versions in {a.group}")
    arn = pkgs[0]["ModelPackageArn"]
    d = sm.describe_model_package(ModelPackageName=arn)
    print("Approving:", arn)
    for k, v in d.get("CustomerMetadataProperties", {}).items():
        print(f"  {k:14s} {v}")
    sm.update_model_package(ModelPackageArn=arn, ModelApprovalStatus="Approved", ApprovalDescription=a.note)
    print("Approved. EventBridge will dispatch deploy.yml.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--group", default="credit-pd")
    p.add_argument("--note", default="Approved by Model Risk (demo)")
    main(p.parse_args())
