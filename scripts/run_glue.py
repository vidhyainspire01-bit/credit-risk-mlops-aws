"""Start a Glue job and wait for it. Exit non-zero on failure (blocks `make data`)."""

import argparse
import sys
import time

import boto3

p = argparse.ArgumentParser()
p.add_argument("--job", required=True)
a = p.parse_args()

glue = boto3.client("glue")
run_id = glue.start_job_run(JobName=a.job)["JobRunId"]
print("Started", a.job, run_id)
state = "STARTING"
while state in ("STARTING", "RUNNING", "WAITING", "STOPPING"):
    time.sleep(20)
    run = glue.get_job_run(JobName=a.job, RunId=run_id)["JobRun"]
    state = run["JobRunState"]
    print(state)
if state != "SUCCEEDED":
    sys.exit(f"Glue job {state}: {run.get('ErrorMessage', '')}")
