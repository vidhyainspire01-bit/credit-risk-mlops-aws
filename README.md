# credit-risk-mlops-aws

Reference implementation of the WeRize model onboarding standard, using the **credit risk probability-of-default (PD) model** as the first onboarded model: one standard path on AWS from data to a governed, monitored, self-retraining real-time model. Every later model reuses this template. The environment is created with `make up` and removed with `make down`.

Rebuilt on a public dataset in a separate AWS account; production code and customer data remain with WeRize.

- Target architecture: [`docs/architecture/credit-risk-target-architecture.drawio`](docs/architecture/credit-risk-target-architecture.drawio)

## The standard path

```
feature/* --PR--> develop --PR--> main
   |                 |               |
 pr.yml           develop.yml     release.yml
 lint, tests,     full pipeline   release pipeline -> credit-pd (PendingManualApproval)
 IaC + image      -> credit-pd-dev        |
 scans, smoke                     Model Risk approves (registry)
 training                                 |
 (SageMaker image)               EventBridge -> Lambda -> deploy.yml
                                  staging (parity, contract, load) -> CAB -> prod
                                  shadow -> canary (auto-rollback) -> A/B -> full
                                          |
                                  Model Monitor + Clarify + CloudWatch -> drift alarm
                                          |
                                  Step Functions: triage -> parallel candidates -> best one pending
```

## Repository layout

| Path | What it is |
|---|---|
| `src/credit_features/` | Shared feature library, used by training AND the endpoint (no train/serve skew) |
| `pipelines/` | SageMaker Pipeline definition + step scripts (preprocess, train, evaluate) |
| `config/pipeline_params.yaml` | One pipeline, per-environment parameters, retrain strategy matrix |
| `docker/` | One image for processing, training and serving |
| `serving/` | `inference.py` (model_fn / input_fn / predict_fn / output_fn), HTTP server, CloudFormation endpoint template |
| `lambdas/` | predict_proxy, approval_dispatch, drift_triage, compare_candidates, traffic_generator, daily_report |
| `stepfunctions/` | Retrain state machine |
| `monitoring/` | Model Monitor schedules, custom PSI job |
| `infra/terraform/` | Platform IaC (bootstrap state, KMS, S3, ECR, IAM/OIDC, registry, Glue, MLflow, API, alarms, dashboard) |
| `.github/workflows/` | pr, develop, release, deploy, infra |
| `scripts/` | data landing, Feature Store ingest, pipeline runner, approve, staging checks, cost check |

## Quick start

```bash
cp infra/terraform/demo.tfvars.example infra/terraform/demo.tfvars   # set email + repo
make doctor
make bootstrap          # once
make up                 # infra + data + image + first release candidate
make approve            # Model Risk approval -> deploy.yml (or: make deploy-staging deploy-prod)
make monitoring-up traffic-on
make score              # one live request
# ... a day later
make down               # everything removed, cost check runs
```

## Model charter (G0)

| Item | Value |
|---|---|
| Objective | Rank applicants by probability of default to support approve / review / decline |
| Label | `default_next_month` (dataset); production target would be 90+ DPD within 12 MOB |
| Risk tier | 1 (drives a credit decision) |
| Explainability | Top-5 reason codes per decision from exact TreeSHAP |
| Fairness | SEX and MARRIAGE excluded from features; monitored as Clarify facets (DPPL tolerance 0.10) |
| Fallback | Any model-path failure returns MANUAL_REVIEW, never auto-approve |
| Latency | p95 < 300 ms end to end |

## Architecture decisions

| ADR | Decision | Why |
|---|---|---|
| 001 | Terraform for the platform, CloudFormation for endpoint stacks | Endpoints change per model version; native SageMaker blue/green + rollback; keeps model releases out of platform state |
| 002 | Deploy only from a Model Registry version | The registry approval is the single control point and audit record |
| 003 | One image, deployed by digest | The exact image tested in staging is the one serving prod |
| 004 | Decision thresholds in SSM, not in the model | Credit policy changes without retraining or redeploying |
| 005 | Drift creates candidates, humans approve | Tier 1 model: automation removes waiting, not the decision |
| 006 | Monotonic constraints on risk features | Behaviour and reason codes stay consistent with credit logic |

## POC vs production

Single AWS account (production: Dev / Shared / Train / Staging / Prod), public UCI dataset with a synthetic application month, HTTP API with IAM auth instead of private API Gateway + mTLS, EventBridge Scheduler instead of MWAA, compressed shadow and A/B windows, simulated ground truth. The POC document lists each difference.

## Not covered by this POC

Multi-account deployment, private networking with VPC endpoints, CIBIL bureau integration, and RBI / DPDP Act control mapping beyond the governance controls shown.
