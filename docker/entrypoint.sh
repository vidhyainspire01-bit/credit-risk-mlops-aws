#!/bin/sh
# SageMaker runs the container as `<image> train` for training jobs and `<image> serve` for endpoints.
# Processing jobs override the entrypoint with `python3 <script>`.
set -e
case "$1" in
  train) exec python3 /opt/program/train.py ;;
  serve) exec gunicorn --bind 0.0.0.0:8080 --workers "${WORKERS:-2}" --timeout 60 serve:app ;;
  *)     exec "$@" ;;
esac
