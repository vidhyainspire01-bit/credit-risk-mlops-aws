# Alarms: paging (applicant impact now) vs drift (feeds the retrain workflow + morning report).

locals {
  drift_alarms = {
    # Model Monitor (native, hourly): completeness of a key history field -> data quality issue
    "credit-pd-data-quality-violations" = {
      namespace = "aws/sagemaker/Endpoints/data-metrics", metric = "feature_non_null_bill_amt1"
      dims = { Endpoint = "credit-pd-prod", MonitoringSchedule = "credit-pd-data-quality" }
      threshold = 0.98, stat = "Minimum", lower = true
    }
    # Model Monitor (native, hourly): distribution distance vs training baseline -> covariate drift
    "credit-pd-feature-drift" = {
      namespace = "aws/sagemaker/Endpoints/data-metrics", metric = "feature_baseline_drift_credit_limit"
      dims = { Endpoint = "credit-pd-prod", MonitoringSchedule = "credit-pd-data-quality" }
      threshold = 0.2, stat = "Maximum"
    }
    # Custom PSI job (monitoring/psi_job.py, `make psi`) -> population shift in the PD score
    "credit-pd-score-drift" = {
      namespace = "CreditPD/Drift", metric = "ScorePSI", dims = {}, threshold = 0.25, stat = "Maximum"
    }
    "credit-pd-model-quality" = {
      namespace = "aws/sagemaker/Endpoints/model-metrics", metric = "auc"
      dims = { Endpoint = "credit-pd-prod", MonitoringSchedule = "credit-pd-model-quality" }
      threshold = 0.70, stat = "Minimum", lower = true
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "drift" {
  for_each            = local.drift_alarms
  alarm_name          = each.key
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dims
  statistic           = each.value.stat
  period              = 3600
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = lookup(each.value, "lower", false) ? "LessThanThreshold" : "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn] # notification; EventBridge rule starts the retrain workflow
}

resource "aws_cloudwatch_metric_alarm" "fallback" {
  alarm_name          = "credit-pd-fallback-rate"
  namespace           = "CreditPD/API"
  metric_name         = "Fallback"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "freshness" {
  alarm_name          = "credit-pd-data-freshness"
  namespace           = "CreditPD/Data"
  metric_name         = "FreshnessLagSeconds"
  dimensions          = { Table = "credit_curated" }
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 86400
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "ops" {
  dashboard_name = "credit-pd-operations"
  dashboard_body = jsonencode({
    widgets = [
      { type = "metric", x = 0, y = 0, width = 8, height = 6, properties = {
        title = "Invocations and errors (prod)", region = var.region, stat = "Sum", period = 300
        metrics = [
          ["AWS/SageMaker", "Invocations", "EndpointName", "credit-pd-prod", "VariantName", "champion"],
          [".", "Invocations", ".", ".", ".", "challenger"],
          [".", "Invocation5XXErrors", ".", ".", ".", "champion"],
        ] } },
      { type = "metric", x = 8, y = 0, width = 8, height = 6, properties = {
        title = "Latency p95 (ms)", region = var.region, period = 300
        metrics = [
          ["AWS/SageMaker", "ModelLatency", "EndpointName", "credit-pd-prod", "VariantName", "champion", { stat = "p95" }],
          ["CreditPD/API", "Latency", { stat = "p95" }],
        ] } },
      { type = "metric", x = 16, y = 0, width = 8, height = 6, properties = {
        title = "Decisions", region = var.region, stat = "Sum", period = 3600
        metrics = [
          ["CreditPD/API", "Decision_APPROVE"], [".", "Decision_MANUAL_REVIEW"], [".", "Decision_DECLINE"], [".", "Fallback"],
        ] } },
      { type = "metric", x = 0, y = 6, width = 12, height = 6, properties = {
        title = "Drift", region = var.region, stat = "Maximum", period = 3600
        metrics = [
          ["aws/sagemaker/Endpoints/data-metrics", "feature_baseline_drift_credit_limit", "Endpoint", "credit-pd-prod",
           "MonitoringSchedule", "credit-pd-data-quality"],
          ["CreditPD/Drift", "ScorePSI"], [".", "MaxFeaturePSI"],
        ]
        annotations = { horizontal = [{ value = 0.1, label = "investigate" }, { value = 0.25, label = "drift" }] }
      } },
      { type = "metric", x = 12, y = 6, width = 12, height = 6, properties = {
        title = "Model quality (matured labels)", region = var.region, stat = "Average", period = 3600
        metrics = [["aws/sagemaker/Endpoints/model-metrics", "auc", "Endpoint", "credit-pd-prod",
                    "MonitoringSchedule", "credit-pd-model-quality"]]
      } },
    ]
  })
}
