# ---------------- GitHub OIDC: no stored AWS keys ----------------
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  ci_roles = {
    dev     = "repo:${var.github_repo}:ref:refs/heads/develop" # integration training only
    release = "repo:${var.github_repo}:ref:refs/heads/main"    # release training only
    deploy  = "repo:${var.github_repo}:environment:*"          # deploy jobs run in environments
    infra   = "repo:${var.github_repo}:*"
  }
}

data "aws_iam_policy_document" "ci_trust" {
  for_each = local.ci_roles
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [each.value]
    }
  }
}

resource "aws_iam_role" "ci" {
  for_each           = local.ci_roles
  name               = "${local.name}-ci-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.ci_trust[each.key].json
}

# ---------------- SageMaker execution role (pipelines, endpoints, monitors) ----------------
data "aws_iam_policy_document" "sm_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sagemaker" {
  name               = "${local.name}-sagemaker-exec"
  assume_role_policy = data.aws_iam_policy_document.sm_trust.json
}

data "aws_iam_policy_document" "sagemaker" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.lake.arn, "${aws_s3_bucket.lake.arn}/*"]
  }
  statement {
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey", "kms:CreateGrant"]
    resources = [aws_kms_key.ml.arn]
  }
  statement {
    actions   = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = ["*"]
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "cloudwatch:PutMetricData"]
    resources = ["*"]
  }
  statement {
    actions = ["sagemaker:*Model*", "sagemaker:*Processing*", "sagemaker:*Training*", "sagemaker:*Pipeline*",
      "sagemaker:*FeatureGroup*", "sagemaker:PutRecord", "sagemaker:GetRecord", "sagemaker:AddTags",
      "sagemaker:*Endpoint*", "sagemaker:*Monitoring*", "sagemaker:*Lineage*", "sagemaker:*Artifact*",
    "sagemaker:*Context*", "sagemaker:*Action*", "sagemaker:*Association*", "sagemaker:Search"]
    resources = ["*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.sagemaker.arn]
  }
  statement {
    actions   = ["glue:*Table*", "glue:*Database*", "glue:GetPartitions"]
    resources = ["*"] # Feature Store offline catalog
  }
}

resource "aws_iam_role_policy" "sagemaker" {
  role   = aws_iam_role.sagemaker.id
  policy = data.aws_iam_policy_document.sagemaker.json
}

# CI roles reuse the SageMaker permissions (demo simplification) plus ECR push and CFN deploy.
data "aws_iam_policy_document" "ci" {
  source_policy_documents = [data.aws_iam_policy_document.sagemaker.json]
  statement {
    actions   = ["ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload"]
    resources = [aws_ecr_repository.image.arn]
  }
  statement {
    actions   = ["cloudformation:*", "application-autoscaling:*", "cloudwatch:*Alarm*", "dynamodb:PutItem"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ci" {
  for_each = aws_iam_role.ci
  role     = each.value.id
  policy   = data.aws_iam_policy_document.ci.json
}

resource "aws_iam_role_policy_attachment" "ci_infra_admin" {
  role       = aws_iam_role.ci["infra"].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess" # demo only; production = scoped permission boundary
}

# ---------------- Separation of duties ----------------
# Nobody except the Model Risk role can set ModelApprovalStatus=Approved on the CENTRAL group.
# Phase 0: confirm the sagemaker:ModelApprovalStatus condition key in the IAM service authorization
# reference; if unavailable, deny sagemaker:UpdateModelPackage on the central group outright.
data "aws_iam_policy_document" "deny_self_approval" {
  statement {
    effect    = "Deny"
    actions   = ["sagemaker:UpdateModelPackage"]
    resources = ["arn:aws:sagemaker:${var.region}:${local.account_id}:model-package/${local.name}/*"]
    condition {
      test     = "StringEquals"
      variable = "sagemaker:ModelApprovalStatus"
      values   = ["Approved"]
    }
  }
}

resource "aws_iam_role_policy" "deny_self_approval" {
  for_each = merge(aws_iam_role.ci, { sagemaker = aws_iam_role.sagemaker })
  role     = each.value.id
  policy   = data.aws_iam_policy_document.deny_self_approval.json
}

data "aws_iam_policy_document" "model_risk_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role" "model_risk" {
  name               = "${local.name}-model-risk-approver"
  assume_role_policy = data.aws_iam_policy_document.model_risk_trust.json
}

resource "aws_iam_role_policy" "model_risk" {
  role = aws_iam_role.model_risk.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["sagemaker:UpdateModelPackage", "sagemaker:DescribeModelPackage", "sagemaker:ListModelPackages",
      "sagemaker:DescribeModelPackageGroup", "s3:GetObject", "kms:Decrypt"]
      Resource = "*"
    }]
  })
}
