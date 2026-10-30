# One image for processing, training and serving. Built once per commit, deployed by digest.
# Production: BASE_IMAGE = the platform team's golden image in ECR (scanned, patched),
# and pip resolves through CodeArtifact with hash checking.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel && pip install -r /tmp/requirements.txt

# Shared feature library (same code in training and serving)
COPY src /opt/src
RUN pip install /opt/src

# Build-time self-test: the pinned numpy / pandas / pyarrow must round-trip a partitioned parquet dataset
RUN python3 -c "import pandas as pd, tempfile; d = tempfile.mkdtemp(); \
pd.DataFrame({'a': [1, 2], 'm': [1, 2]}).to_parquet(d, partition_cols=['m']); \
assert set(pd.read_parquet(d).columns) == {'a', 'm'}; print('parquet self-test ok')"

# Hardening: no package manager or bundled installer wheels in the runtime image
# (removes pip's vendored urllib3/msgpack and the ensurepip setuptools wheel from the attack surface)
RUN pip uninstall -y pip && rm -rf /usr/local/lib/python3.11/ensurepip

COPY pipelines/steps/train.py /opt/program/train.py
COPY serving/inference.py /opt/program/inference.py
COPY serving/serve.py /opt/program/serve.py
COPY docker/entrypoint.sh /opt/program/entrypoint.sh
RUN chmod +x /opt/program/entrypoint.sh && useradd -u 1000 -m app

WORKDIR /opt/program
ENV PATH="/opt/program:${PATH}"
ENTRYPOINT ["/opt/program/entrypoint.sh"]
