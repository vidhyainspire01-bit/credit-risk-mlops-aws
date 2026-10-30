# Data, serving glue, orchestration and observability around the SageMaker core.

# ---------------- Data: Glue curation job ----------------
resource "aws_glue_catalog_database" "credit" {
  name = "credit_pd"
}

resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.lake.id
  key    = "code/glue/curate_credit.py"
  source = "${path.module}/../../data/glue/curate_credit.py"
  etag   = filemd5("${path.module}/../../data/glue/curate_credit.py")
}

resource "aws_iam_role" "glue" {
  name = "${local.name}-glue"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
  Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "glue.amazonaws.com" } }] })
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_data" {
  role   = aws_iam_role.glue.id
  policy = data.aws_iam_policy_document.sagemaker.json
}

resource "aws_glue_job" "curate" {
  name              = "${local.name}-curate"
  role_arn          = aws_iam_role.glue.arn
  glue_version      = "4.0"
  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 30
  command {
    script_location = "s3://${aws_s3_bucket.lake.id}/${aws_s3_object.glue_script.key}"
    python_version  = "3"
  }
  default_arguments = {
    "--RAW_PATH"                         = "s3://${aws_s3_bucket.lake.id}/raw/"
    "--CURATED_PATH"                     = "s3://${aws_s3_bucket.lake.id}/curated/credit/"
    "--QUARANTINE_PATH"                  = "s3://${aws_s3_bucket.lake.id}/quarantine/credit/"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
  }
}

# ---------------- Experiment tracking ----------------
resource "aws_sagemaker_mlflow_tracking_server" "mlflow" {
  count                        = var.create_mlflow ? 1 : 0
  tracking_server_name         = "${local.name}-mlflow"
  artifact_store_uri           = "s3://${aws_s3_bucket.lake.id}/mlflow"
  role_arn                     = aws_iam_role.sagemaker.arn
  tracking_server_size         = "Small"
  automatic_model_registration = false # registry entries come only from the governed pipeline
}

# ---------------- Lambda packaging helper ----------------
locals {
  lambdas = {
    predict_proxy = {
      timeout = 5, memory = 512, env = {
        ENDPOINT_NAME   = "credit-pd-prod", DECISION_LOG_BUCKET = aws_s3_bucket.lake.id
        THRESHOLD_PARAM = aws_ssm_parameter.thresholds.name, FEATURE_GROUP = "credit-customer-history"
    } }
    approval_dispatch = {
      timeout = 15, memory = 256, env = {
        GITHUB_REPO  = var.github_repo, GITHUB_TOKEN_SECRET = aws_secretsmanager_secret.github_token.name
        DEPLOY_GROUP = aws_sagemaker_model_package_group.central.model_package_group_name
    } }
    drift_triage = {
      timeout = 30, memory = 256, env = { PIPELINE_NAME = "credit-pd-training-release", COOLDOWN_HOURS = "6" }
    }
    compare_candidates = {
      timeout = 60, memory = 256, env = {
        MODEL_PACKAGE_GROUP = aws_sagemaker_model_package_group.central.model_package_group_name
        ARTIFACT_BUCKET     = aws_s3_bucket.lake.id
    } }
    traffic_generator = {
      timeout = 120, memory = 256, env = {
        API_URL = "${aws_apigatewayv2_api.score.api_endpoint}/score", ARTIFACT_BUCKET = aws_s3_bucket.lake.id
    } }
    daily_report = {
      timeout = 120, memory = 512, env = {
        DECISION_LOG_BUCKET = aws_s3_bucket.lake.id, SNS_TOPIC_ARN = aws_sns_topic.alerts.arn
        ENDPOINT_NAME       = "credit-pd-prod"
    } }
  }
}

data "archive_file" "fn" {
  for_each    = local.lambdas
  type        = "zip"
  source_dir  = "${path.module}/../../lambdas/${each.key}"
  output_path = "${path.module}/.build/${each.key}.zip"
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
  Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }] })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sagemaker:InvokeEndpoint", "sagemaker:GetRecord", "sagemaker:ListModelPackages",
        "sagemaker:DescribeModelPackage", "sagemaker:UpdateModelPackage", "sagemaker:ListPipelineExecutions"],
      Resource = "*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      Resource = [aws_s3_bucket.lake.arn, "${aws_s3_bucket.lake.arn}/*"] },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.ml.arn },
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${local.name}/*" },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.github_token.arn },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData", "cloudwatch:GetMetricStatistics"], Resource = "*" },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
      { Effect = "Allow", Action = ["execute-api:Invoke"], Resource = "${aws_apigatewayv2_api.score.execution_arn}/*" },
    ]
  })
}

# Lambdas may only move a version to Rejected (compare_candidates), never Approved.
resource "aws_iam_role_policy" "lambda_deny_approve" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.deny_self_approval.json
}

resource "aws_lambda_function" "fn" {
  for_each         = local.lambdas
  function_name    = "${local.name}-${replace(each.key, "_", "-")}"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.fn[each.key].output_path
  source_code_hash = data.archive_file.fn[each.key].output_base64sha256
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  kms_key_arn      = aws_kms_key.ml.arn
  environment { variables = each.value.env }
  tracing_config { mode = "Active" }
}

# ---------------- Scoring API (HTTP API, IAM auth) ----------------
resource "aws_apigatewayv2_api" "score" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "score" {
  api_id                 = aws_apigatewayv2_api.score.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn["predict_proxy"].invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 3000
}

resource "aws_apigatewayv2_route" "score" {
  api_id             = aws_apigatewayv2_api.score.id
  route_key          = "POST /score"
  target             = "integrations/${aws_apigatewayv2_integration.score.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.score.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 20
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId            = "$context.requestId", ip = "$context.identity.sourceIp", caller = "$context.identity.caller",
      route                = "$context.routeKey", status = "$context.status", latencyMs = "$context.responseLatency",
      integrationLatencyMs = "$context.integrationLatency"
    })
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["predict_proxy"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.score.execution_arn}/*/*"
}

# ---------------- Registry approval -> deploy ----------------
resource "aws_cloudwatch_event_rule" "approved" {
  name = "${local.name}-model-approved"
  event_pattern = jsonencode({
    source        = ["aws.sagemaker"]
    "detail-type" = ["SageMaker Model Package State Change"]
    detail = {
      ModelPackageGroupName = [aws_sagemaker_model_package_group.central.model_package_group_name]
      ModelApprovalStatus   = ["Approved"]
    }
  })
}

resource "aws_cloudwatch_event_target" "approved" {
  rule = aws_cloudwatch_event_rule.approved.name
  arn  = aws_lambda_function.fn["approval_dispatch"].arn
}

resource "aws_lambda_permission" "approved" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["approval_dispatch"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.approved.arn
}

# ---------------- Drift -> Step Functions retrain ----------------
resource "aws_iam_role" "sfn" {
  name = "${local.name}-sfn"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
  Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "states.amazonaws.com" } }] })
}

resource "aws_iam_role_policy" "sfn" {
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction",
      Resource = [aws_lambda_function.fn["drift_triage"].arn, aws_lambda_function.fn["compare_candidates"].arn] },
      { Effect = "Allow", Action = ["sagemaker:StartPipelineExecution", "sagemaker:DescribePipelineExecution"], Resource = "*" },
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.alerts.arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.ml.arn },
      { Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
      "xray:GetSamplingTargets"], Resource = "*" },
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups"], Resource = "*" },
    ]
  })
}

resource "aws_sfn_state_machine" "retrain" {
  name     = "${local.name}-retrain"
  role_arn = aws_iam_role.sfn.arn
  tracing_configuration { enabled = true }
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
  definition = templatefile("${path.module}/../../stepfunctions/retrain.asl.json", {
    triage_lambda_arn  = aws_lambda_function.fn["drift_triage"].arn
    compare_lambda_arn = aws_lambda_function.fn["compare_candidates"].arn
    sns_topic_arn      = aws_sns_topic.alerts.arn
    pipeline_name      = "credit-pd-training-release"
  })
}

resource "aws_iam_role" "events_sfn" {
  name = "${local.name}-events-sfn"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
  Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "events.amazonaws.com" } }] })
}

resource "aws_iam_role_policy" "events_sfn" {
  role = aws_iam_role.events_sfn.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
  Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.retrain.arn }] })
}

resource "aws_cloudwatch_event_rule" "drift" {
  name = "${local.name}-drift-alarm"
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [for a in keys(local.drift_alarms) : a]
      state     = { value = ["ALARM"] }
    }
  })
}

resource "aws_cloudwatch_event_target" "drift" {
  rule     = aws_cloudwatch_event_rule.drift.name
  arn      = aws_sfn_state_machine.retrain.arn
  role_arn = aws_iam_role.events_sfn.arn
}

# ---------------- Schedules: traffic + daily report ----------------
resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
  Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" } }] })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "lambda:InvokeFunction",
  Resource = [aws_lambda_function.fn["traffic_generator"].arn, aws_lambda_function.fn["daily_report"].arn] }] })
}

resource "aws_scheduler_schedule" "traffic" {
  name                = "${local.name}-traffic"
  schedule_expression = "rate(5 minutes)"
  state               = var.traffic_enabled ? "ENABLED" : "DISABLED"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.fn["traffic_generator"].arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "daily_report" {
  name                         = "${local.name}-daily-report"
  schedule_expression          = "cron(0 9 * * ? *)"
  schedule_expression_timezone = "Asia/Kolkata"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.fn["daily_report"].arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
