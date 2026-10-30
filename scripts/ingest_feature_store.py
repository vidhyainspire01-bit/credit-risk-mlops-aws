"""Phase 1: ingest customer repayment history into SageMaker Feature Store.

Feature group `credit-customer-history` (online + offline):
  record id: customer_id, event time: event_time
  features: pay_0..pay_6, bill_amt1..6, pay_amt1..6

Application fields (credit_limit, education, age) are NOT stored here; they arrive
in the scoring request, exactly as in a real loan origination flow.

Usage: python scripts/ingest_feature_store.py --curated s3://.../curated/ --offline s3://.../offline --role-arn ...
"""

import argparse
import time

import pandas as pd
import sagemaker
from sagemaker.feature_store.feature_definition import FeatureDefinition, FeatureTypeEnum
from sagemaker.feature_store.feature_group import FeatureGroup

from credit_features import HISTORY_FIELDS

FG_NAME = "credit-customer-history"


def main(curated: str, offline: str, role_arn: str, kms_key: str | None) -> None:
    sess = sagemaker.Session()
    df = pd.read_parquet(curated)
    cols = ["customer_id", "event_time"] + HISTORY_FIELDS
    df = df[cols].copy()
    df["customer_id"] = df["customer_id"].astype(str)
    df["event_time"] = df["event_time"].astype(str)
    for c in HISTORY_FIELDS:
        df[c] = df[c].astype("float64")

    fg = FeatureGroup(name=FG_NAME, sagemaker_session=sess)
    fg.feature_definitions = [
        FeatureDefinition("customer_id", FeatureTypeEnum.STRING),
        FeatureDefinition("event_time", FeatureTypeEnum.STRING),
    ] + [FeatureDefinition(c, FeatureTypeEnum.FRACTIONAL) for c in HISTORY_FIELDS]

    existing = {g["FeatureGroupName"] for g in sess.sagemaker_client.list_feature_groups()["FeatureGroupSummaries"]}
    if FG_NAME not in existing:
        fg.create(
            s3_uri=offline,
            record_identifier_name="customer_id",
            event_time_feature_name="event_time",
            role_arn=role_arn,
            enable_online_store=True,
            online_store_kms_key_id=kms_key,
            offline_store_kms_key_id=kms_key,
            tags=[{"Key": "Project", "Value": "credit-pd-demo"}],
        )
        while fg.describe()["FeatureGroupStatus"] == "Creating":
            time.sleep(10)
        print("Feature group created:", fg.describe()["FeatureGroupStatus"])

    fg.ingest(data_frame=df, max_workers=8, wait=True)
    print(f"Ingested {len(df)} records into {FG_NAME}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--curated", required=True)
    p.add_argument("--offline", required=True)
    p.add_argument("--role-arn", required=True)
    p.add_argument("--kms-key", default=None)
    a = p.parse_args()
    main(a.curated, a.offline, a.role_arn, a.kms_key)
