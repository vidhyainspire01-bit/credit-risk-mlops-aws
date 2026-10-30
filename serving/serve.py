"""Minimal SageMaker-compatible HTTP server: GET /ping, POST /invocations on port 8080."""

import os

from flask import Flask, Response, request

import inference

MODEL_DIR = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")
app = Flask(__name__)
_model = inference.model_fn(MODEL_DIR)  # loaded once per worker at startup


@app.get("/ping")
def ping():
    return Response(status=200 if _model else 503)


@app.post("/invocations")
def invocations():
    try:
        x = inference.input_fn(request.get_data(), request.content_type or "application/json")
    except (ValueError, KeyError) as e:
        return Response(f'{{"error": "{e}"}}', status=400, mimetype="application/json")
    pred = inference.predict_fn(x, _model)
    return Response(inference.output_fn(pred), status=200, mimetype="application/json")
