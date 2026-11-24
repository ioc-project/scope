FROM python:3.13-slim-trixie AS builder

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir "uv==0.12.10"

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.13 \
    UV_PROJECT_ENVIRONMENT=/app

WORKDIR /src/integrations/cognition-fabric/ioc-analytics-api

COPY --from=scope_backend mce/mce-core /src/backend/mce/mce-core
COPY --from=scope_backend mce/mce-provider-native /src/backend/mce/mce-provider-native
COPY --from=scope_backend mce/mce-provider-sdk /src/backend/mce/mce-provider-sdk
COPY --from=ontology . /src/ontology
COPY pyproject.toml uv.lock README.md ./
COPY api ./api

RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --frozen --no-dev --no-editable --package oxp-api

FROM python:3.13-slim-trixie AS runner

RUN useradd -m user

# Copy venv from builder with just the dependencies we need + our package
COPY --from=builder --chown=user:user /app /app

USER user

EXPOSE 8000

CMD ["/app/bin/dev"]