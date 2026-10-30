"""Create Model Monitor schedules on the prod endpoint using the baselines attached to the
deployed registry version (drift_check_baselines), plus model-quality with ground truth.

  python monitoring/setup_monitors.py --endpoint credit-pd-prod --model-package-arn <arn>

Schedules (hourly in the demo; hourly / daily in production per the monitoring table):
  credit-pd-data-quality   : input feature distributions vs training baseline
  credit-pd-model-quality  : AUC etc. once ground truth is merged (InferenceId = application_id)
CloudWatch metrics emitted by the monitors drive the drift alarms in Terraform.

If Model Monitor cannot parse the capture format in your region, use monitoring/psi_job.py
(custom PSI processing job) as the fallback; it publishes the same CloudWatch metric names.
"""

import argparse
import os

import boto3
from sagemaker.model_monitor import CronExpressionGenerator, DefaultModelMonitor, EndpointInput, ModelQualityMonitor

TAGS = [{"Key": "Project", "Value": "credit-pd-demo"}]


def main(a):
    role = os.environ["SAGEMAKER_ROLE_ARN"]
    bucket = os.environ["ARTIFACT_BUCKET"]
    sm = boto3.client("sagemaker")
    pkg = sm.describe_model_package(ModelPackageName=a.model_package_arn)
    base = pkg["DriftCheckBaselines"]["ModelDataQuality"]

    for name in ("credit-pd-data-quality", "credit-pd-model-quality"):
        try:
            sm.delete_monitoring_schedule(MonitoringScheduleName=name)
        except sm.exceptions.ResourceNotFound:
            pass

    dq = DefaultModelMonitor(
        role=role, instance_count=1, instance_type="ml.m5.large", max_runtime_in_seconds=1800, tags=TAGS
    )
    dq.create_monitoring_schedule(
        monitor_schedule_name="credit-pd-data-quality",
        endpoint_input=a.endpoint,
        output_s3_uri=f"s3://{bucket}/monitoring/data-quality",
        statistics=base["Statistics"]["S3Uri"],
        constraints=base["Constraints"]["S3Uri"],
        schedule_cron_expression=CronExpressionGenerator.hourly(),
        enable_cloudwatch_metrics=True,
    )

    mq = ModelQualityMonitor(
        role=role, instance_count=1, instance_type="ml.m5.large", max_runtime_in_seconds=1800, tags=TAGS
    )
    mq.create_monitoring_schedule(
        monitor_schedule_name="credit-pd-model-quality",
        endpoint_input=EndpointInput(
            endpoint_name=a.endpoint,
            destination="/opt/ml/processing/input_data",
            probability_attribute="pd",
            probability_threshold_attribute=0.5,
        ),
        ground_truth_input=f"s3://{bucket}/groundtruth",
        problem_type="BinaryClassification",
        output_s3_uri=f"s3://{bucket}/monitoring/model-quality",
        schedule_cron_expression=CronExpressionGenerator.hourly(),
        enable_cloudwatch_metrics=True,
    )
    print("Monitoring schedules created for", a.endpoint)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default="credit-pd-prod")
    p.add_argument("--model-package-arn", required=True)
    main(p.parse_args())
