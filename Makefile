# One interface for the whole lifecycle. `make up` builds, `make down` destroys.
SHELL := /bin/bash
TF     := terraform -chdir=infra/terraform
REGION ?= ap-south-1
export AWS_REGION := $(REGION)
export AWS_DEFAULT_REGION := $(REGION)
PYTHONPATH := src:.
export PYTHONPATH

out = $(shell $(TF) output -raw $(1) 2>/dev/null)
ARTIFACT_BUCKET    = $(call out,artifact_bucket)
SAGEMAKER_ROLE_ARN = $(call out,sagemaker_role_arn)
ECR                = $(call out,ecr_repository_url)
KMS                = $(call out,kms_key_arn)
SHA               := $(shell git rev-parse --short HEAD 2>/dev/null || echo local)

.PHONY: help preflight doctor bootstrap infra-up data image pipeline-dev pipeline-release approve deploy-staging \
        deploy-prod monitoring-up traffic-on traffic-off drift-on drift-off psi retrain-now score \
        down cost-check up test smoke

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

preflight:  ## read-only: service availability + SageMaker quotas in the region (costs nothing)
	python3 scripts/preflight.py --region $(REGION)

doctor:  ## check local tools and AWS identity
	@terraform -version | head -1 && aws --version && python3 --version && docker --version
	@aws sts get-caller-identity --query Arn --output text

test:  ## unit tests
	pytest tests -q

smoke:  ## PR smoke training locally: same step scripts, same image, synthetic data (Docker only, no AWS)
	python3 tests/make_fixture.py
	docker build -f docker/Dockerfile -t credit-pd:smoke .
	bash tests/smoke_train.sh credit-pd:smoke

bootstrap:  ## one-time remote state (kept after `make down`)
	terraform -chdir=infra/terraform/bootstrap init && terraform -chdir=infra/terraform/bootstrap apply -auto-approve
	terraform -chdir=infra/terraform/bootstrap output -raw backend_hcl > infra/terraform/backend.hcl

infra-up:  ## platform: KMS, S3, ECR, IAM/OIDC, registry groups, Glue, MLflow, Lambdas, API, Step Functions, alarms
	$(TF) init -backend-config=backend.hcl -upgrade
	$(TF) apply -auto-approve -var-file=demo.tfvars

data:  ## land raw -> Glue curate with DQ -> Feature Store -> golden set + traffic sample
	python3 scripts/download_data.py --bucket $(ARTIFACT_BUCKET)
	python3 scripts/run_glue.py --job credit-pd-curate
	python3 scripts/ingest_feature_store.py --curated s3://$(ARTIFACT_BUCKET)/curated/credit/ \
	  --offline s3://$(ARTIFACT_BUCKET)/featurestore --role-arn $(SAGEMAKER_ROLE_ARN) --kms-key $(KMS)
	python3 scripts/make_golden.py --curated s3://$(ARTIFACT_BUCKET)/curated/credit/ --bucket $(ARTIFACT_BUCKET)

image:  ## build + push image (tag = git SHA), print digest
	aws ecr get-login-password | docker login --username AWS --password-stdin $(firstword $(subst /, ,$(ECR)))
	docker build -f docker/Dockerfile -t $(ECR):$(SHA) . && docker push $(ECR):$(SHA)
	docker inspect --format='{{index .RepoDigests 0}}' $(ECR):$(SHA) > .image_uri && cat .image_uri

pipeline-dev:  ## integration training -> credit-pd-dev group
	SAGEMAKER_ROLE_ARN=$(SAGEMAKER_ROLE_ARN) IMAGE_URI=$$(cat .image_uri) ARTIFACT_BUCKET=$(ARTIFACT_BUCKET) \
	  python3 scripts/run_pipeline.py --env dev --wait

pipeline-release:  ## release training -> credit-pd group (PendingManualApproval)
	SAGEMAKER_ROLE_ARN=$(SAGEMAKER_ROLE_ARN) IMAGE_URI=$$(cat .image_uri) ARTIFACT_BUCKET=$(ARTIFACT_BUCKET) \
	  python3 scripts/run_pipeline.py --env release --wait

approve:  ## role-play Model Risk approval of latest pending version (triggers deploy.yml)
	python3 scripts/approve.py --group credit-pd

LATEST_APPROVED = $(shell aws sagemaker list-model-packages --model-package-group-name credit-pd \
  --model-approval-status Approved --sort-by CreationTime --sort-order Descending --max-results 1 \
  --query 'ModelPackageSummaryList[0].ModelPackageArn' --output text)

deploy-staging:  ## local equivalent of deploy.yml staging job
	aws cloudformation deploy --stack-name credit-pd-endpoint-staging --template-file serving/cfn/endpoint.yaml \
	  --no-fail-on-empty-changeset --tags Project=credit-pd-demo Env=staging --parameter-overrides EnvName=staging \
	  ModelPackageArn=$(LATEST_APPROVED) Mode=single ExecutionRoleArn=$(SAGEMAKER_ROLE_ARN) KmsKeyArn=$(KMS) \
	  CaptureS3Uri=s3://$(ARTIFACT_BUCKET)/datacapture/staging
	python3 scripts/staging_checks.py --endpoint credit-pd-staging --model-package-arn $(LATEST_APPROVED)

MODE ?= single
CHAMPION ?= $(LATEST_APPROVED)
CHALLENGER ?=
deploy-prod:  ## MODE=single|shadow|ab [CHAMPION=arn CHALLENGER=arn]
	aws cloudformation deploy --stack-name credit-pd-endpoint-prod --template-file serving/cfn/endpoint.yaml \
	  --no-fail-on-empty-changeset --tags Project=credit-pd-demo Env=prod --parameter-overrides EnvName=prod \
	  ModelPackageArn=$(CHAMPION) ChallengerPackageArn=$(CHALLENGER) Mode=$(MODE) \
	  ExecutionRoleArn=$(SAGEMAKER_ROLE_ARN) KmsKeyArn=$(KMS) CaptureS3Uri=s3://$(ARTIFACT_BUCKET)/datacapture/prod

monitoring-up:  ## Model Monitor schedules on prod from the deployed version's baselines
	SAGEMAKER_ROLE_ARN=$(SAGEMAKER_ROLE_ARN) ARTIFACT_BUCKET=$(ARTIFACT_BUCKET) \
	  python3 monitoring/setup_monitors.py --endpoint credit-pd-prod --model-package-arn $(CHAMPION)

traffic-on:  ## start simulated loan applications every 5 minutes
	$(TF) apply -auto-approve -var-file=demo.tfvars -var traffic_enabled=true
traffic-off:
	$(TF) apply -auto-approve -var-file=demo.tfvars -var traffic_enabled=false

drift-on:  ## shift incoming population (lower limits -> higher utilisation, more defaults)
	aws ssm put-parameter --name /credit-pd/drift-mode --value on --overwrite
drift-off:
	aws ssm put-parameter --name /credit-pd/drift-mode --value off --overwrite

psi:  ## custom PSI drift job over the last 6h of capture (publishes CreditPD/Drift metrics)
	python3 monitoring/psi_job.py --capture s3://$(ARTIFACT_BUCKET)/datacapture/prod/ \
	  --baseline s3://$(ARTIFACT_BUCKET)/$$(aws s3 ls s3://$(ARTIFACT_BUCKET)/pipeline/ --recursive | grep baseline.jsonl | tail -1 | awk '{print $$4}')

retrain-now:  ## start the retrain state machine manually (DRIFT=covariate|population|concept)
	aws stepfunctions start-execution --state-machine-arn $(call out,retrain_state_machine_arn) \
	  --input '{"force":"$(or $(DRIFT),population)"}' --query executionArn --output text

score:  ## one signed request to the live API
	python3 -c "import json,boto3,urllib.request as u; from botocore.auth import SigV4Auth; \
	from botocore.awsrequest import AWSRequest; url='$(call out,api_url)'; \
	b=json.dumps({'application_id':'APP-DEMO-1','customer_id':'25001','credit_limit':50000,'education':2,'age':35}); \
	r=AWSRequest(method='POST',url=url,data=b,headers={'Content-Type':'application/json'}); \
	s=boto3.Session(); SigV4Auth(s.get_credentials(),'execute-api',s.region_name).add_auth(r); \
	print(json.dumps(json.loads(u.urlopen(u.Request(url,data=b.encode(),headers=dict(r.headers),method='POST')).read()),indent=2))"

up: infra-up data image pipeline-release  ## build platform, data and the first release candidate
	@echo "Next: make approve (deploy.yml runs), or make deploy-staging / deploy-prod locally"

down:  ## delete endpoints, monitors, feature group, then all Terraform infra
	-for s in credit-pd-data-quality credit-pd-model-quality; do aws sagemaker delete-monitoring-schedule --monitoring-schedule-name $$s; done
	-aws cloudformation delete-stack --stack-name credit-pd-endpoint-prod
	-aws cloudformation delete-stack --stack-name credit-pd-endpoint-staging
	-aws cloudformation wait stack-delete-complete --stack-name credit-pd-endpoint-prod
	-aws cloudformation wait stack-delete-complete --stack-name credit-pd-endpoint-staging
	-aws sagemaker delete-feature-group --feature-group-name credit-customer-history
	-for p in dev release; do aws sagemaker delete-pipeline --pipeline-name credit-pd-training-$$p; done
	-for g in credit-pd credit-pd-dev; do \
	  for a in $$(aws sagemaker list-model-packages --model-package-group-name $$g --query 'ModelPackageSummaryList[].ModelPackageArn' --output text); do \
	    aws sagemaker delete-model-package --model-package-name $$a; done; done
	$(TF) destroy -auto-approve -var-file=demo.tfvars
	$(MAKE) cost-check

cost-check:  ## fail if anything billable is left
	python3 scripts/cost_check.py
