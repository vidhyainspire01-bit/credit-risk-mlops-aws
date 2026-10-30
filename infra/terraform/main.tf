terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 5.70" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
  }
  backend "s3" {} # backend.hcl: bucket, key, region, dynamodb_table (created by bootstrap/)
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project    = "credit-pd-demo"
      CostCentre = "mlops-demo"
      ModelId    = "credit-pd"
      ManagedBy  = "terraform"
    }
  }
}

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}

locals {
  name       = "credit-pd"
  account_id = data.aws_caller_identity.me.account_id
  bucket     = "${local.name}-${local.account_id}-${var.region}"
}

# ---------------- Encryption + storage ----------------
resource "aws_kms_key" "ml" {
  description             = "CMK for credit PD data, models, endpoints, logs"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "AccountAdmin", Effect = "Allow", Principal = { AWS = "arn:aws:iam::${local.account_id}:root" },
      Action = "kms:*", Resource = "*" },
      { Sid    = "CloudWatchLogs", Effect = "Allow", Principal = { Service = "logs.${var.region}.amazonaws.com" },
        Action = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
        Resource = "*",
      Condition = { ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.region}:${local.account_id}:*" } } },
    ]
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/credit-pd-api"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.ml.arn
}

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/states/credit-pd-retrain"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.ml.arn
}

resource "aws_kms_alias" "ml" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.ml.key_id
}

resource "aws_s3_bucket" "lake" {
  bucket        = local.bucket
  force_destroy = true # demo only: allows one-command teardown
}

resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.ml.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "expire-capture"
    status = "Enabled"
    filter { prefix = "datacapture/" }
    expiration { days = 30 }
  }
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

# ---------------- Container registry ----------------
resource "aws_ecr_repository" "image" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ml.arn
  }
}

resource "aws_ecr_lifecycle_policy" "image" {
  repository = aws_ecr_repository.image.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep last 20 images",
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 },
    action       = { type = "expire" }
  }] })
}

# ---------------- Model registry groups ----------------
resource "aws_sagemaker_model_package_group" "dev" {
  model_package_group_name        = "${local.name}-dev"
  model_package_group_description = "Integration candidates from develop. Never deployed."
}

resource "aws_sagemaker_model_package_group" "central" {
  model_package_group_name        = local.name
  model_package_group_description = "Release candidates from main. Tier 1. Deploy only after Model Risk approval."
}

# ---------------- Model inventory (model risk view) ----------------
resource "aws_dynamodb_table" "inventory" {
  name         = "${local.name}-model-inventory"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "model_package_arn"
  range_key    = "deployed_at"
  attribute {
    name = "model_package_arn"
    type = "S"
  }
  attribute {
    name = "deployed_at"
    type = "S"
  }
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.ml.arn
  }
  point_in_time_recovery { enabled = true } # the inventory is audit evidence
}

# ---------------- Policy config: decision thresholds live outside the model ----------------
resource "aws_ssm_parameter" "thresholds" {
  name = "/${local.name}/thresholds"
  type = "String"
  value = jsonencode({
    approve_below = 0.15, decline_from = 0.45, min_age = 21, max_limit = 1000000
  })
}

resource "aws_ssm_parameter" "drift_mode" {
  name  = "/${local.name}/drift-mode"
  type  = "String"
  value = "off"
  lifecycle { ignore_changes = [value] } # toggled by `make drift-on/off`
}

resource "aws_secretsmanager_secret" "github_token" {
  name                    = "${local.name}/github-dispatch-token"
  kms_key_id              = aws_kms_key.ml.arn
  recovery_window_in_days = 0
}

# ---------------- Alerts ----------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = aws_kms_key.ml.id
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_budgets_budget" "demo" {
  name         = "${local.name}-demo"
  budget_type  = "COST"
  limit_amount = "100"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$credit-pd-demo"]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
