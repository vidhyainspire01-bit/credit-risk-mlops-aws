# One image for processing, training and serving. Built once per commit, deployed by digest.
# In production: FROM the platform team's golden base image; pip resolves via CodeArtifact.
FROM public.ecr.aws/docker/library/python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Shared feature library (same code in training and serving)
COPY src /opt/src
RUN pip install /opt/src

COPY pipelines/steps/train.py /opt/program/train.py
COPY serving/inference.py /opt/program/inference.py
COPY serving/serve.py /opt/program/serve.py
COPY docker/entrypoint.sh /opt/program/entrypoint.sh
RUN chmod +x /opt/program/entrypoint.sh && useradd -u 1000 -m app

WORKDIR /opt/program
ENV PATH="/opt/program:${PATH}"
ENTRYPOINT ["/opt/program/entrypoint.sh"]
