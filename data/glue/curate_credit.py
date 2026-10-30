"""Glue job: raw -> curated, with Glue Data Quality.

- Casts types, removes duplicate customer_id rows
- Evaluates a DQDL ruleset; failing rows go to a quarantine prefix
- Writes curated Parquet partitioned by application_month
- Publishes a freshness metric to CloudWatch

Job args: --RAW_PATH --CURATED_PATH --QUARANTINE_PATH
"""
import sys
from datetime import datetime, timezone

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from awsgluedq.transforms import EvaluateDataQuality
from pyspark.context import SparkContext
from pyspark.sql import functions as F

args = getResolvedOptions(sys.argv, ["JOB_NAME", "RAW_PATH", "CURATED_PATH", "QUARANTINE_PATH"])
sc = SparkContext()
glue = GlueContext(sc)
spark = glue.spark_session
job = Job(glue)
job.init(args["JOB_NAME"], args)

df = spark.read.option("header", True).option("inferSchema", True).csv(args["RAW_PATH"])
df = df.withColumn("customer_id", F.col("customer_id").cast("string")).dropDuplicates(["customer_id"])

RULESET = """
Rules = [
    IsComplete "customer_id",
    IsUnique "customer_id",
    IsComplete "credit_limit",
    ColumnValues "credit_limit" > 0,
    ColumnValues "age" between 18 and 100,
    ColumnValues "default_next_month" in [0, 1],
    ColumnValues "pay_0" between -2 and 9,
    Completeness "bill_amt1" > 0.99,
    RowCount > 1000
]
"""

dyf = DynamicFrame.fromDF(df, glue, "raw")
dq = EvaluateDataQuality().process_rows(
    frame=dyf,
    ruleset=RULESET,
    publishing_options={
        "dataQualityEvaluationContext": "credit_curate",
        "enableDataQualityCloudWatchMetrics": True,
        "enableDataQualityResultsPublishing": True,
    },
)
rows = dq["rowLevelOutcomes"].toDF()
good = rows.filter(F.col("DataQualityEvaluationResult") == "Passed")
bad = rows.filter(F.col("DataQualityEvaluationResult") != "Passed")

dq_cols = [c for c in rows.columns if c.startswith("DataQuality")]
good.drop(*dq_cols).write.mode("overwrite").partitionBy("application_month").parquet(args["CURATED_PATH"])
if bad.count() > 0:
    bad.write.mode("append").parquet(args["QUARANTINE_PATH"])

# Freshness: seconds since the newest landed record (demo data has a single event_time).
latest = df.agg(F.max("event_time")).collect()[0][0]
lag = (datetime.now(timezone.utc) - datetime.fromisoformat(str(latest).replace("Z", "+00:00"))).total_seconds()
boto3.client("cloudwatch").put_metric_data(
    Namespace="CreditPD/Data",
    MetricData=[
        {"MetricName": "FreshnessLagSeconds", "Value": lag, "Unit": "Seconds",
         "Dimensions": [{"Name": "Table", "Value": "credit_curated"}]},
        {"MetricName": "QuarantinedRows", "Value": float(bad.count()), "Unit": "Count",
         "Dimensions": [{"Name": "Table", "Value": "credit_curated"}]},
    ],
)
job.commit()
