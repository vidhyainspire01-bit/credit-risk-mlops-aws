output "artifact_bucket" { value = aws_s3_bucket.lake.id }
output "kms_key_arn" { value = aws_kms_key.ml.arn }
output "ecr_repository_url" { value = aws_ecr_repository.image.repository_url }
output "sagemaker_role_arn" { value = aws_iam_role.sagemaker.arn }
output "model_risk_role_arn" { value = aws_iam_role.model_risk.arn }
output "ci_role_arns" { value = { for k, r in aws_iam_role.ci : k => r.arn } }
output "api_url" { value = "${aws_apigatewayv2_api.score.api_endpoint}/score" }
output "retrain_state_machine_arn" { value = aws_sfn_state_machine.retrain.arn }
output "sns_topic_arn" { value = aws_sns_topic.alerts.arn }
output "mlflow_tracking_arn" {
  value = var.create_mlflow ? aws_sagemaker_mlflow_tracking_server.mlflow[0].arn : null
}
