# IOC Analytics API

The analytics data-access layer and REST API for OXP. The Python package
`oxp-api` (import name `scope`) provides application, session, and span analytics
over OpenTelemetry traces in ClickHouse and enriched knowledge-graph data in
Neo4j. Consumers can use FastAPI endpoints or the in-process `LocalClient`.

Use a separate Python environment from `ioc-scope-api`: both distributions
provide a top-level `scope` package.

## Capabilities

- **Trace analytics:** applications, sessions, spans, timelines, live topology,
  and session labels.
- **Graph-backed analysis:** session state, embeddings, similar-session search,
  concepts, semantic groups, reasoning paths, impact assessments, and waste
  insights. These views require graph data populated by upstream processing.
- **Metric computation:** read stored metrics or compute them through
  [SCOPE's MCE packages](../../../backend/mce).
- **Consumer integration:** REST endpoints under `/api/v1` and `/api/v2`,
  typed Python models, and `GoldenClient` reference responses for testing
  without databases.

## Tech stack

Python 3.13+, FastAPI, Uvicorn, Pydantic, clickhouse-connect, SQLAlchemy,
the Neo4j driver, and optional Redis response caching.

## Quick start

Run these commands from `ioc-analytics-api/`, not the enclosing repository root:

```bash
cd ioc-analytics-api
uv sync --project api
```

Install [uv](https://docs.astral.sh/uv/) first. The workspace uses the local
[SCOPE ontology](../../../ontology) and MCE packages.

Configure your existing backends, then start the server:

```bash
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USERNAME=admin
export CLICKHOUSE_PASSWORD='replace-with-your-clickhouse-password'
export CLICKHOUSE_DATABASE=oxp
export NEO4J_HOST=localhost
export NEO4J_PORT=7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD='replace-with-your-neo4j-password'
export NEO4J_DATABASE=neo4j

uv run --project api dev
```

The API is available at `http://127.0.0.1:8000`, interactive OpenAPI documentation
at `/docs`, and the health probe at `/healthz`. The `dev` entry point also loads
a local `.env` file. Set `OXP_API_PORT` to change the port and
`OXP_API_RELOAD=false` to disable development auto-reload.

ClickHouse schemas are in [`api/data/clickhouse/`](api/data/clickhouse/).
Redis caching is disabled by default; enable it with `CACHE_ENABLED=true` and
configure `CACHE_REDIS_URL` for your Redis instance.

## Using as a Python library

`LocalClient` accesses the databases directly, without an HTTP server:

```python
from scope import LocalClient

with LocalClient.from_settings() as client:
    applications = client.get_application_names()
    sessions = client.list_sessions(limit=10)
```

See the [API package README](api/README.md) for explicit connector configuration,
client methods, response types, and metric-provider integration.

## Docker

Build from this workspace directory, using the SCOPE source directories as
additional BuildKit contexts:

```bash
docker buildx build --load -f api/Dockerfile \
  --build-context scope_backend=../../../backend \
  --build-context ontology=../../../ontology \
  -t ioc-analytics-api .
```

Run with a `.env` file containing backend settings reachable from the container
(use `host.docker.internal` for databases running on a Docker Desktop host):

```bash
docker run --rm -p 8000:8000 --env-file .env \
  -e OXP_API_RELOAD=false \
  ioc-analytics-api
```

## Development

### Tests

From this workspace directory:

```bash
uv sync --project api --group dev
uv run --project api --group dev --no-sync python -m pytest api/tests/ -v
```

The root pytest configuration targets `api/tests`, not the other OXP platform
packages. Many API tests require live, populated ClickHouse or Neo4j backends;
see the [API testing instructions](api/README.md#running-tests) before running
the suite.

### Formatting and linting

```bash
uv run ruff format api
uv run ruff check api
```

## Workspace contents

| Path | Purpose |
|------|---------|
| [`api/`](api/README.md) | REST server, in-process client, connectors, query builders, and API tests |
| [`api/clients/`](api/clients/README.md) | Generated HTTP client tooling and usage |
| [SCOPE MCE](../../../backend/mce) | Shared metric engine and providers |
| [`pyproject.toml`](pyproject.toml) | uv workspace members, dependency sources, and root test configuration |

This workspace serves analytics data; it does not contain the full OXP
normalization, enrichment, or worker pipeline.
