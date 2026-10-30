variable "region" {
  type        = string
  default     = "ap-south-1"
  description = "Mumbai region: RBI data localisation and DPDP Act obligations."
}

variable "alert_email" {
  type        = string
  description = "SNS alert + budget email"
}

variable "github_repo" {
  type        = string
  description = "owner/repo for OIDC trust and repository_dispatch"
}

variable "create_mlflow" {
  type    = bool
  default = true
}

variable "traffic_enabled" {
  type        = bool
  default     = false
  description = "Set true after the prod endpoint exists (make traffic-on)"
}
