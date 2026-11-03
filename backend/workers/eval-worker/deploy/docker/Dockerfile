#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

# Build with the backend/ directory as the context (see docker-bake.hcl).
# mce and stateful-eval workers, in one process.

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
COPY workers/eval-worker /app/workers/eval-worker
COPY workers/mce-worker /app/workers/mce-worker
COPY workers/stateful-eval-worker /app/workers/stateful-eval-worker
COPY worker-base /app/worker-base
COPY mce /app/mce
COPY stateful-evals /app/stateful-evals
COPY api /app/api
COPY --from=ontology . /ontology

RUN uv sync --package eval-worker --frozen --no-editable --no-dev

ARG MODEL_PRICES_REFRESHED_AT=unset
COPY scripts/update_model_prices.py /tmp/update_model_prices.py
RUN /opt/venv/bin/python /tmp/update_model_prices.py \
      --trimmed-file "$(/opt/venv/bin/python -c 'import pathlib, mce.engine as p; print(pathlib.Path(p.__file__).parent / "data" / "model_prices.json")')" \
    || echo "WARNING: could not refresh the LLM price table of the MCE from LiteLLM; keeping the one committed in the repo"
RUN /opt/venv/bin/python /tmp/update_model_prices.py \
      --full-file "$(/opt/venv/bin/python -c 'import pathlib, stateful_evals_be as p; print(pathlib.Path(p.__file__).parent / "data" / "model_prices_and_context_window.json")')" \
    || echo "WARNING: could not refresh the LLM price table of the stateful evals from LiteLLM; keeping the one committed in the repo"

# ── Run stage ─────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # The stateful evaluation's metrics (mdt-metrics) are plugins of another engine: the MCE would import them, and
    # litellm with them (about 300 MB), only to skip them.
    MCE_IGNORED_PLUGIN_PACKAGES=mdt-metrics

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY workers/eval-worker/deploy/docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
