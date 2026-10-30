#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

# Build with the backend/ directory as the context (see docker-bake.hcl).
# grouping, hierarchical grouping, analysis and intelligence workers: the first three in one process, and analysis as a
# child process (it forks a pool of CPU-bound processes of its own).

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
COPY workers/semantic-analysis-worker /app/workers/semantic-analysis-worker
COPY workers/grouping-worker /app/workers/grouping-worker
COPY workers/hierarchical-grouping-worker /app/workers/hierarchical-grouping-worker
COPY workers/analysis-worker /app/workers/analysis-worker
COPY workers/intelligence-worker /app/workers/intelligence-worker
COPY worker-base /app/worker-base
COPY dem /app/dem
COPY mce /app/mce
COPY api /app/api
COPY intelligence-catalog /app/intelligence-catalog
COPY --from=ontology . /ontology

RUN uv sync --package semantic-analysis-worker --frozen --no-editable --no-dev

# ── Run stage ─────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/intelligence-catalog /app/intelligence-catalog
COPY workers/semantic-analysis-worker/deploy/docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
