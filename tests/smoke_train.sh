#!/usr/bin/env bash
# PR smoke training (G2): run the SAME pipeline step scripts inside the SAME image SageMaker uses,
# on a synthetic 1,200-row fixture, with the SageMaker /opt/ml directory contract.
# Needs only Docker: no AWS credentials, no cost. Proves preprocess -> train -> evaluate -> serve works.
#
#   bash tests/smoke_train.sh credit-pd:smoke
set -euo pipefail
IMAGE="${1:-credit-pd:smoke}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK"/processing/input "$WORK"/input/config "$WORK"/input/data
cp tests/fixtures/credit_sample.csv "$WORK"/processing/input/
echo '{"num_round": "30", "max_depth": "3", "eta": "0.1"}' > "$WORK"/input/config/hyperparameters.json

run() { docker run --rm -v "$PWD":/w -v "$WORK":/opt/ml -w /w "$IMAGE" "$@"; }

echo "== preprocess (out-of-time split, feature re-selection path)"
run python3 pipelines/steps/preprocess.py --feature-set reselect --sample-weight-decay 0.1

echo "== train (training channel contract)"
cp -r "$WORK"/processing/train "$WORK"/input/data/train
cp -r "$WORK"/processing/validation "$WORK"/input/data/validation
run train

echo "== evaluate (model.tar.gz contract)"
mkdir -p "$WORK"/processing/model
tar -czf "$WORK"/processing/model/model.tar.gz -C "$WORK"/model .
run python3 pipelines/steps/evaluate.py

echo "== serve the trained artefact"
docker run -d --name smoke-svc -p 8081:8080 -v "$WORK"/model:/opt/ml/model "$IMAGE" serve >/dev/null
trap 'docker rm -f smoke-svc >/dev/null 2>&1; rm -rf "$WORK"' EXIT
for _ in $(seq 1 20); do curl -sf localhost:8081/ping >/dev/null && break; sleep 1; done
curl -sf -H "Content-Type: application/json" -d @tests/sample_request.json localhost:8081/invocations | tee "$WORK"/out.json
echo
python3 - "$WORK"/out.json "$WORK"/processing/evaluation/evaluation.json <<'EOF'
import json, sys
out, ev = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
assert 0 <= out["pd"] <= 1 and isinstance(out["reason_codes"], list), out
m = ev["binary_classification_metrics"]
assert 0 <= m["auc"]["value"] <= 1 and "dppl_sex" in ev["fairness"], ev
print(f"Smoke training passed: OOT AUC {m['auc']['value']}, KS {m['ks']['value']}, PD {out['pd']}")
EOF
