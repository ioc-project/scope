#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

# Build with the backend/ directory as the context (see docker-bake.hcl).
# norm and embedding workers, in one process.

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

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
COPY workers/ingestion-worker /app/workers/ingestion-worker
COPY workers/norm-worker /app/workers/norm-worker
COPY workers/embedding-worker /app/workers/embedding-worker
COPY worker-base /app/worker-base
COPY norm /app/norm
COPY dem /app/dem
COPY mce /app/mce
COPY api /app/api
COPY --from=ontology . /ontology

RUN uv sync --package ingestion-worker --frozen --no-editable --no-dev

# ── Run stage ─────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY workers/ingestion-worker/deploy/docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
