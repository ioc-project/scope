#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

# API-only image: the embeddings come from an OpenAI-compatible (or Google) endpoint, so torch and the other
# local-model libraries (the dem[embedding-local] extra) are not installed. To run a SentenceTransformer, Qwen3
# or gensim embedder, build a variant with that extra (and CPU-only torch wheels, to avoid the CUDA runtime).

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:${PATH}"

COPY pyproject.toml uv.lock /app/
COPY workers/embedding-worker /app/workers/embedding-worker
COPY worker-base /app/worker-base
COPY dem /app/dem
COPY mce /app/mce
COPY api /app/api
COPY --from=ontology . /ontology

RUN uv sync --package embedding-worker --frozen --no-editable --no-dev \
    && find /opt/venv -type d \( -name __pycache__ -o -name tests -o -name test \) -prune -exec rm -rf {} +

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY workers/embedding-worker/deploy/docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
