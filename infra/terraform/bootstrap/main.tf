# One-time: remote state bucket + lock table. Run once with local state, then never destroyed by `make down`.
terraform {
  required_providers { aws = { source = "hashicorp/aws", version = ">= 5.70" } }
}

variable "region" { default = "ap-south-1" }
provider "aws" { region = var.region }
data "aws_caller_identity" "me" {}

resource "aws_s3_bucket" "state" {
  bucket = "credit-pd-tfstate-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "lock" {
  name         = "credit-pd-tflock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
}

output "backend_hcl" {
  value = <<-EOT
    bucket         = "${aws_s3_bucket.state.id}"
    key            = "credit-pd/demo.tfstate"
    region         = "${var.region}"
    dynamodb_table = "${aws_dynamodb_table.lock.name}"
    encrypt        = true
  EOT
}
