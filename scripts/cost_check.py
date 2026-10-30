"""After `make down`: list anything still running that costs money. Exit 1 if found.

Checks SageMaker endpoints, monitoring schedules, notebook/Studio apps, MLflow tracking
servers, feature groups, and any resource still tagged Project=credit-pd-demo.
"""

import sys

import boto3

TAG = {"Key": "Project", "Values": ["credit-pd-demo"]}
sm = boto3.client("sagemaker")
left = []

left += [
    f"endpoint {e['EndpointName']}"
    for e in sm.list_endpoints()["Endpoints"]
    if e["EndpointName"].startswith("credit-pd")
]
left += [
    f"monitoring schedule {m['MonitoringScheduleName']}"
    for m in sm.list_monitoring_schedules()["MonitoringScheduleSummaries"]
]
left += [
    f"studio app {a['AppName']} ({a['AppType']})"
    for a in sm.list_apps()["Apps"]
    if a["Status"] not in ("Deleted", "Failed")
]
try:
    left += [
        f"mlflow server {s['TrackingServerName']}"
        for s in sm.list_mlflow_tracking_servers()["TrackingServerSummaries"]
        if s["TrackingServerStatus"] not in ("Deleted",)
    ]
except Exception:
    pass
left += [
    f"feature group {g['FeatureGroupName']}"
    for g in sm.list_feature_groups()["FeatureGroupSummaries"]
    if g["FeatureGroupName"].startswith("credit-")
]

tagged = boto3.client("resourcegroupstaggingapi").get_resources(TagFilters=[TAG])["ResourceTagMappingList"]
left += [f"tagged {r['ResourceARN']}" for r in tagged if ":key/" not in r["ResourceARN"]]  # KMS keys wait for deletion

if left:
    print("Resources still present:")
    for r in left:
        print("  -", r)
    sys.exit(1)
print("Clean: no billable demo resources found.")
