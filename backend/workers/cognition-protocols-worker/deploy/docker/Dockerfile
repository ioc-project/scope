#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

# Build with the backend/ directory as the context (see docker-bake.hcl). The two protocol
# MCE-metrics packages (ACCORD, CONCORD) are git dependencies on the public cognition-protocols repository.

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt

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

COPY pyproject.toml uv.lock /opt/
COPY workers/cognition-protocols-worker /opt/workers/cognition-protocols-worker
COPY worker-base /opt/worker-base
COPY mce /opt/mce
COPY api /opt/api
COPY --from=ontology . /ontology

RUN uv sync --package cognition-protocols-worker --frozen --no-editable --no-dev

# ── Run stage ─────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt

COPY --from=builder /opt/.venv /opt/.venv
COPY workers/cognition-protocols-worker/deploy/docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
