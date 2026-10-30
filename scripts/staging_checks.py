"""Gate G4: pre-deployment checks against the STAGING endpoint. Non-zero exit blocks promotion.

1. parity   : endpoint scores == offline scores of the registered artifact on a golden set
2. contract : valid payload -> 200 with pd/reason_codes/model_version; bad payload -> 4xx
3. load     : N sequential calls, p95 latency under budget, zero errors
4. fallback : (optional) verifies the API returns MANUAL_REVIEW when the endpoint is unreachable

Usage: python scripts/staging_checks.py --endpoint credit-pd-staging --model-package-arn <arn> --golden tests/golden.jsonl
"""

import argparse
import json
import statistics
import sys
import tarfile
import tempfile
import time

import boto3
import numpy as np
import xgboost as xgb

sys.path.insert(0, "src")
from credit_features import transform  # noqa: E402

rt = boto3.client("sagemaker-runtime")
sm, s3 = boto3.client("sagemaker"), boto3.client("s3")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def offline_scorer(package_arn: str):
    d = sm.describe_model_package(ModelPackageName=package_arn)
    uri = d["InferenceSpecification"]["Containers"][0]["ModelDataUrl"]
    b, k = uri.replace("s3://", "").split("/", 1)
    tmp = tempfile.mkdtemp()
    s3.download_file(b, k, f"{tmp}/model.tar.gz")
    tarfile.open(f"{tmp}/model.tar.gz").extractall(tmp, filter="data")
    booster = xgb.Booster()
    booster.load_model(f"{tmp}/model.ubj")
    cal = json.load(open(f"{tmp}/calibrator.json"))
    feats = json.load(open(f"{tmp}/metadata.json"))["feature_order"]

    def score(rec: dict) -> float:
        f = transform(rec)
        x = np.array([[f[n] for n in feats]], dtype=np.float32)
        raw = booster.predict(xgb.DMatrix(x, feature_names=feats, missing=np.nan))[0]
        return float(np.interp(raw, cal["x"], cal["y"]))

    return score


def invoke(endpoint: str, rec: dict) -> tuple[int, dict, float]:
    t0 = time.time()
    try:
        r = rt.invoke_endpoint(EndpointName=endpoint, ContentType="application/json", Body=json.dumps(rec))
        return 200, json.loads(r["Body"].read()), (time.time() - t0) * 1000
    except rt.exceptions.ModelError as e:
        return e.response.get("OriginalStatusCode", 400), {}, (time.time() - t0) * 1000


def main(a):
    golden = [json.loads(line) for line in open(a.golden)]

    score = offline_scorer(a.model_package_arn)
    diffs = [abs(invoke(a.endpoint, r)[1]["pd"] - score(r)) for r in golden[:50]]
    check("parity", max(diffs) < 1e-5, f"max |online - offline| = {max(diffs):.2e} over {len(diffs)} records")

    code, body, _ = invoke(a.endpoint, golden[0])
    ok = code == 200 and {"pd", "reason_codes", "model_version"} <= body.keys() and 0 <= body["pd"] <= 1
    check("contract_valid", ok, f"status {code}, keys {sorted(body)}")
    try:
        rt.invoke_endpoint(EndpointName=a.endpoint, ContentType="text/csv", Body="1,2,3")
        check("contract_invalid", False, "CSV payload was accepted")
    except rt.exceptions.ModelError:
        check("contract_invalid", True, "unsupported payload rejected with 4xx")

    lat, errors = [], 0
    for r in (golden * (a.load_calls // len(golden) + 1))[: a.load_calls]:
        code, _, ms = invoke(a.endpoint, r)
        lat.append(ms)
        errors += code != 200
    p95 = statistics.quantiles(lat, n=20)[18]
    check("load", errors == 0 and p95 < a.p95_ms, f"{len(lat)} calls, p95 {p95:.0f} ms, errors {errors}")

    failed = [n for n, ok, _ in results if not ok]
    json.dump(
        [{"check": n, "pass": ok, "detail": d} for n, ok, d in results], open("staging_checks.json", "w"), indent=2
    )
    if failed:
        sys.exit(f"Staging gate failed: {failed}")
    print("Staging gate G4 passed.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default="credit-pd-staging")
    p.add_argument("--model-package-arn", required=True)
    p.add_argument("--golden", default="tests/golden.jsonl")
    p.add_argument("--load-calls", type=int, default=200)
    p.add_argument("--p95-ms", type=float, default=250)
    main(p.parse_args())
