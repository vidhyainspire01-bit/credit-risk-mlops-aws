"""SageMaker Pipeline: one definition for smoke (local mode), dev and release.

preprocess -> train -> evaluate -> [Clarify data bias] -> [data-quality baseline]
           -> condition (KS floor, champion, fairness) -> register (with provenance)
"""

from __future__ import annotations

from sagemaker import clarify
from sagemaker.drift_check_baselines import DriftCheckBaselines
from sagemaker.estimator import Estimator
from sagemaker.inputs import TrainingInput
from sagemaker.model import Model
from sagemaker.model_metrics import MetricsSource, ModelMetrics
from sagemaker.model_monitor.dataset_format import DatasetFormat
from sagemaker.processing import ProcessingInput, ProcessingOutput, ScriptProcessor
from sagemaker.workflow.check_job_config import CheckJobConfig
from sagemaker.workflow.clarify_check_step import ClarifyCheckStep, DataBiasCheckConfig
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo, ConditionLessThanOrEqualTo
from sagemaker.workflow.fail_step import FailStep
from sagemaker.workflow.functions import Join, JsonGet
from sagemaker.workflow.model_step import ModelStep
from sagemaker.workflow.parameters import ParameterFloat, ParameterString
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.quality_check_step import DataQualityCheckConfig, QualityCheckStep
from sagemaker.workflow.steps import ProcessingStep, TrainingStep

from credit_features import LABEL


def build(
    *,
    env_name: str,
    env_cfg: dict,
    common: dict,
    role: str,
    image_uri: str,
    bucket: str,
    session,
    model_package_group: str | None,
) -> Pipeline:
    local = env_cfg.get("local_mode", False)
    instance = "local" if local else env_cfg.get("instance_type", "ml.m5.large")
    prefix = f"s3://{bucket}/pipeline"

    # ---- Parameters (set per run by scripts/run_pipeline.py) ----
    p_input = ParameterString("InputDataUri", default_value=f"s3://{bucket}/curated/credit/")
    p_train_months = ParameterString("TrainMonths", default_value="1-10")
    p_decay = ParameterFloat("SampleWeightDecay", default_value=0.0)
    p_feature_set = ParameterString("FeatureSet", default_value="champion")
    p_champion_ks = ParameterFloat("ChampionKS", default_value=0.0)
    p_git_sha = ParameterString("GitSha", default_value="local")
    p_mlflow_run = ParameterString("MlflowRunId", default_value="none")
    p_candidate = ParameterString("CandidateId", default_value="baseline")
    p_approval = ParameterString(
        "ApprovalStatus", default_value=env_cfg.get("approval_status", "PendingManualApproval")
    )
    params = [
        p_input,
        p_train_months,
        p_decay,
        p_feature_set,
        p_champion_ks,
        p_git_sha,
        p_mlflow_run,
        p_candidate,
        p_approval,
    ]

    proc = ScriptProcessor(
        image_uri=image_uri,
        command=["python3"],
        role=role,
        instance_count=1,
        instance_type=instance,
        sagemaker_session=session,
        tags=[{"Key": "Project", "Value": "credit-pd-demo"}],
    )

    # ---- 1. Preprocess ----
    step_pre = ProcessingStep(
        name="Preprocess",
        processor=proc,
        code="pipelines/steps/preprocess.py",
        inputs=[ProcessingInput(source=p_input, destination="/opt/ml/processing/input")],
        outputs=[
            ProcessingOutput(output_name=n, source=f"/opt/ml/processing/{n}")
            for n in ("train", "validation", "oot", "baseline", "bias")
        ],
        job_arguments=[
            "--sample-fraction",
            str(env_cfg.get("sample_fraction", 1.0)),
            "--train-months",
            p_train_months,
            "--oot-months",
            ",".join(map(str, common["oot_months"])),
            "--sample-weight-decay",
            p_decay.to_string(),
            "--feature-set",
            p_feature_set,
        ],
    )
    out = step_pre.properties.ProcessingOutputConfig.Outputs

    # ---- 2. Train ----
    hp = {**common["hyperparameters"]}
    if "num_round" in env_cfg:
        hp["num_round"] = env_cfg["num_round"]
    est = Estimator(
        image_uri=image_uri,
        role=role,
        instance_count=1,
        instance_type=instance,
        hyperparameters=hp,
        output_path=f"{prefix}/models",
        use_spot_instances=env_cfg.get("use_spot", False) and not local,
        max_run=3600,
        max_wait=7200 if env_cfg.get("use_spot") and not local else None,
        checkpoint_s3_uri=None if local else f"{prefix}/checkpoints",
        sagemaker_session=session,
        tags=[{"Key": "Project", "Value": "credit-pd-demo"}],
    )
    step_train = TrainingStep(
        name="Train",
        estimator=est,
        inputs={
            "train": TrainingInput(out["train"].S3Output.S3Uri, content_type="text/csv"),
            "validation": TrainingInput(out["validation"].S3Output.S3Uri, content_type="text/csv"),
        },
    )

    # ---- 3. Evaluate ----
    eval_file = PropertyFile(name="Evaluation", output_name="evaluation", path="evaluation.json")
    step_eval = ProcessingStep(
        name="Evaluate",
        processor=proc,
        code="pipelines/steps/evaluate.py",
        inputs=[
            ProcessingInput(step_train.properties.ModelArtifacts.S3ModelArtifacts, "/opt/ml/processing/model"),
            ProcessingInput(out["oot"].S3Output.S3Uri, "/opt/ml/processing/oot"),
        ],
        outputs=[ProcessingOutput(output_name="evaluation", source="/opt/ml/processing/evaluation")],
        property_files=[eval_file],
    )

    steps = [step_pre, step_train, step_eval]
    baselines = None

    # ---- 4. Clarify pre-training bias + Model Monitor data-quality baseline (not in local mode) ----
    if env_cfg.get("run_clarify") and not local:
        check_cfg = CheckJobConfig(role=role, instance_count=1, instance_type="ml.m5.large", sagemaker_session=session)
        step_bias = ClarifyCheckStep(
            name="DataBiasCheck",
            clarify_check_config=DataBiasCheckConfig(
                data_config=clarify.DataConfig(
                    s3_data_input_path=out["bias"].S3Output.S3Uri,
                    s3_output_path=f"{prefix}/clarify",
                    label=LABEL,
                    dataset_type="text/csv",
                ),
                data_bias_config=clarify.BiasConfig(
                    label_values_or_threshold=[0], facet_name="sex", facet_values_or_threshold=[2]
                ),
            ),
            check_job_config=check_cfg,
            skip_check=True,
            register_new_baseline=True,
            model_package_group_name=model_package_group,
        )
        step_dq = QualityCheckStep(
            name="DataQualityBaseline",
            quality_check_config=DataQualityCheckConfig(
                baseline_dataset=out["baseline"].S3Output.S3Uri,
                dataset_format=DatasetFormat.json(lines=True),
                output_s3_uri=f"{prefix}/baselines",
            ),
            check_job_config=check_cfg,
            skip_check=True,
            register_new_baseline=True,
            model_package_group_name=model_package_group,
        )
        steps += [step_bias, step_dq]
        baselines = DriftCheckBaselines(
            model_data_statistics=MetricsSource(
                s3_uri=step_dq.properties.BaselineUsedForDriftCheckStatistics, content_type="application/json"
            ),
            model_data_constraints=MetricsSource(
                s3_uri=step_dq.properties.BaselineUsedForDriftCheckConstraints, content_type="application/json"
            ),
            bias_pre_training_constraints=MetricsSource(
                s3_uri=step_bias.properties.BaselineUsedForDriftCheckConstraints, content_type="application/json"
            ),
        )

    # ---- 5. Gate + register ----
    ks = JsonGet(step_name=step_eval.name, property_file=eval_file, json_path="binary_classification_metrics.ks.value")
    dppl = JsonGet(step_name=step_eval.name, property_file=eval_file, json_path="fairness.dppl_sex")
    conditions = [
        ConditionGreaterThanOrEqualTo(left=ks, right=common["ks_floor"] if not local else 0.0),
        ConditionGreaterThanOrEqualTo(left=ks, right=p_champion_ks - common["champion_tolerance"]),
        ConditionLessThanOrEqualTo(left=dppl, right=common["bias_dppl_max"] if not local else 1.0),
    ]

    pass_steps = []
    if model_package_group:
        metrics = ModelMetrics(
            model_statistics=MetricsSource(
                s3_uri=Join(
                    on="/",
                    values=[
                        step_eval.properties.ProcessingOutputConfig.Outputs["evaluation"].S3Output.S3Uri,
                        "evaluation.json",
                    ],
                ),
                content_type="application/json",
            )
        )
        model = Model(
            image_uri=image_uri,
            model_data=step_train.properties.ModelArtifacts.S3ModelArtifacts,
            role=role,
            sagemaker_session=session,
        )
        register_args = model.register(
            content_types=["application/json"],
            response_types=["application/json"],
            inference_instances=["ml.m5.large"],
            transform_instances=["ml.m5.large"],
            model_package_group_name=model_package_group,
            approval_status=p_approval,
            model_metrics=metrics,
            drift_check_baselines=baselines,
            customer_metadata_properties={
                "git_sha": p_git_sha,
                "mlflow_run_id": p_mlflow_run,
                "candidate_id": p_candidate,
                "train_months": p_train_months,
                "feature_set": p_feature_set,
                "image_uri": image_uri,
                "risk_tier": "1",
            },
            description="Credit PD model: XGBoost (monotonic) + isotonic calibration",
        )
        pass_steps.append(ModelStep(name="RegisterModel", step_args=register_args))

    steps.append(
        ConditionStep(
            name="QualityGate",
            conditions=conditions,
            if_steps=pass_steps,
            else_steps=[
                FailStep(name="GateFailed", error_message=Join(on=" ", values=["Gate failed. KS:", ks, "DPPL:", dppl]))
            ],
        )
    )

    return Pipeline(
        name=f'{common["pipeline_name"]}-{env_name}', parameters=params, steps=steps, sagemaker_session=session
    )
