# API

This library provides an unified access layer across all the underlying data stored in databases. It fronts ClickHouse/SQLite/PostgreSQL (session, span and trace data) and Neo4j (the knowledge graph).
An optional FastAPI REST server is built on top of the library, as a wrapper that serves the functions as endpoints; thus, it can be used as a single Python library, or as a REST service.

**Source:** [`api`](https://github.com/ioc-project/scope/tree/main/backend/api)

How to use the API:

- **As a library, in-process.** Workers import `scope.dependencies` and `scope.client.dal` directly and talk to Neo4j without ever going through HTTP.
- **As a REST server.** The UI and other external callers hit the FastAPI app (`scope.api:app`), which is a thin wrapper around the same library. Its endpoints are documented by an interactive [Swagger UI](#interactive-api-reference-swagger).

The [platform diagram](../index.md) shows an overview of the system as a block diagram: The workers use the API as a Python library, while the UI connects to it via the REST server.

Business logic therefore lives in the library layer (`scope.client`), not in the FastAPI endpoint functions — see [Contributing](#contributing) below.

## Overview

This component consolidates data-access patterns that used to be spread across `scope-backend`,
`DAL`, and the workflow components into one library.

```text
                         ┌───────────────────────────┐
   HTTP callers  ──────► │   scope.api  (FastAPI app)  │
   (UI, etc.)            │   api_v1 / api_v2 routers │
                         └────────────┬──────────────┘
                                      │ Depends(get_db / get_neo4j_db / get_redis)
                                      ▼
   Workers, scripts  ──────────► scope.client.LocalClient  ◄────── scope.providers
   (in-process import)               │                           (Metrics/KG/Data
                                      │ query_builders → Connector │ providers, bridge
                                      ▼                             to MCE)
                          ┌─────────────────────────────┐
                          │  scope.connectors             │
                          │  ClickHouse / SQLAlchemy    │
                          │  (SQLite, Postgres) / Neo4j │
                          └─────────────────────────────┘
```

## Architecture

### Package layout

| Package | Role |
|---|---|
| `scope/connectors` | `Connector` ABC plus `ClickHouseConnector`, `SQLAlchemyConnector` (SQLite/PostgreSQL), `Neo4JConnector` |
| `scope/query_builders` | Pure functions that build a SQLAlchemy `Select` or a Cypher string + params for one domain (sessions, metrics, kg, semanticgroups, …) — no I/O |
| `scope/providers` | Concrete `MetricsProvider`/`KGProvider`/`DataProvider` implementations; the bridge into MCE's `MetricEngine` for `/metrics/compute` |
| `scope/interfaces` | Abstract provider contracts (`MetricsProvider`, `KGProvider`, `DataProvider`) and their shared Pydantic models |
| `scope/models` | Pydantic response models — the typed contract returned by both the library and the REST API |
| `scope/api` | The FastAPI app; `api_v1` (stable) and `api_v2` (emerging) routers, one module per domain under `api_v1/endpoints/` |
| `scope/core` | `Settings` (`pydantic-settings`, env-var driven) and the `SCOPEError` exception hierarchy |

### The request pipeline

Every read/write, whether called directly from the Python lib, or from an HTTP endpoint, follows the same three
steps (from the `LocalClient` docstring, [`client/local.py`](https://github.com/ioc-project/scope/tree/main/backend/api/scope/client/local.py)):

1. **Query builder** — `scope.query_builders.<domain>` turns typed arguments into a SQLAlchemy
   `Select` (ClickHouse/SQLite/Postgres) or a Cypher string + params (Neo4j).
2. **Connector** — `self.db.execute(stmt)` runs it and returns raw rows.
3. **Model mapping** — rows are mapped onto Pydantic response models from `scope.models.otel_traces`.

### REST layer

`scope.api:app` (in [`scope/api/__init__.py`](https://github.com/ioc-project/scope/tree/main/backend/api/scope/api/__init__.py))
is a thin wrapper: each endpoint depends on `get_db` / `get_neo4j_db` / `get_redis`
(`scope/dependencies.py`), which lazily create **singleton** connectors on first use and reuse them
for the process lifetime — avoiding a new connection pool per request. The
endpoint builds a `LocalClient` view over that connector (`get_client(api_client=db)`) and
delegates straight to the matching sub-client method.

`api_v1` is the stable, versioned surface most endpoints live under; `api_v2` currently only carries
a `metrics` router and is where new incompatible response shapes should land instead of breaking v1.

#### Caching

Some of the endpoints implement complex queries to retrieve or process a considerable amount of data from the databases. At the same time, some clients, like the UI, require responsiveness to human interaction. To speed up the response times, we have implemented a caching mechanism with Redis.  

`cached(redis_client, key, fetch, ttl, enabled)` wraps a query in a Redis
  get/compute/set, keyed by `make_cache_key(prefix, params)` (a SHA-256 of the sorted params). This means that if a query to the same function (endpoint) and with the same parameters arrives, it will be served by the cache. Otherwise, the actual request happens and the cache is updated for the next access.

### Interactive API reference (Swagger)

The API is a FastAPI application, so it publishes its own OpenAPI documentation, generated from the endpoint
definitions and their Pydantic models, with nothing to maintain by hand:

| URL | What |
|---|---|
| `/docs` | **Swagger UI**: browse every endpoint, see its parameters and response models, and call it with *Try it out* |
| `/redoc` | ReDoc: the same reference, as a read-only document |
| `/openapi.json` | The OpenAPI specification, to generate a client or import into another tool |

The endpoints are grouped by tag (`sessions`, `spans`, `metrics`, `kg`, `semanticgroups`, `applications`, `ui`, ...), and
live under `/api/v1` and `/api/v2`. *Try it out* sends real requests, so it needs the databases behind the API to be
reachable.

Where to find it depends on the deployment:

- **Locally**, with `uv run dev`: <http://127.0.0.1:8000/docs>.
- **Kubernetes**: forward the API service, and open <http://localhost:8000/docs>:

    ```bash
    kubectl -n scope port-forward svc/scope-api 8000:8000
    ```

- **Docker Compose**: the API container's port 8000 is not published to the host, and the gateway only forwards
  `/gql`, `/api/v1` and `/api/v1alpha`, so the Swagger pages are not served through it. Reach them from the
  container, or publish the API port.

## Usage

### Run the server

The API runs on its own, from source or in a container, and is configured with a `.env` file. See
[Run the API standalone](standalone.md). The server starts on <http://127.0.0.1:8000>, and its Swagger UI is at
[`/docs`](#interactive-api-reference-swagger).

### Use as a library

```python
from scope import LocalClient

with LocalClient() as client:
    apps = client.get_application_names()
    sessions = client.get_sessions(app_name="my-app", limit=10)
```


## Testing

```bash
uv venv .venv_tests
source .venv_tests/bin/activate
uv sync --active --dev

python -m pytest tests/ -v
```

Note that most of the tests in the `pytest` suite above, run against a live Neo4j/ClickHouse database. Therefore, make sure the system has access to one. Typically, you can either deploy it locally, or host it in a real cluster and use port-forwarding to provide transparent access via local ports. 

Example of `kubectl port-forward` commands:
```
kubectl port-forward -n scope-clickhouse svc/scope-clickhouse 9000:9000 8123:8123
kubectl port-forward -n scope-neo4j svc/scope-neo4j 7474:7474 7687:7687
```

Check folder `api/tests/` for available tests.

`uv sync --active --dev` also pulls the vendored `mce-core`/`mce-provider-*` wheels needed for
real metric compute in tests.


### Vendored wheel maintenance (Docker builds)

The API image builds with Docker `context: api`, so `api/uv.lock` cannot reference sources outside
that directory. When updating `mce-core` or an MCE provider, rebuild the wheels and copy them into
`api/vendor/` before re-locking — see the
[README](https://github.com/ioc-project/scope/tree/main/backend/api/README.md#vendored-wheel-maintenance-important-for-docker-builds)
for the exact commands. Skipping this and pointing `[tool.uv.sources]` at `../mce/...` will build
locally but break the Docker image.

## Contributing

### Adding a new domain or endpoint

The pipeline in [Architecture](#the-request-pipeline) is also the recipe for adding functionality:

1. Add query-building functions to `scope/query_builders/<domain>.py` (pure — no I/O, dialect-aware if needed).
2. Add a `<Domain>Client` mixin in `scope/client/_<domain>.py` that calls the query builder, runs it
   through `self.db.execute(...)`, and maps rows onto a Pydantic model.
3. Mix it into `LocalClient` in `scope/client/local.py`.
4. Add/extend response models in `scope/models/otel_traces.py` (or a new models module for a new domain).
5. If it needs an HTTP surface, add a router in `scope/api/api_v1/endpoints/<domain>.py` that only
   wires up `Depends(get_db | get_neo4j_db)`, an optional `cached(...)` wrapper, and a call into the
   client — no query logic in the endpoint itself — then register it in `scope/api/api_v1/api.py`.

Keep business logic in the client layer, not the endpoint: the same method is used by REST callers
and by anything importing `scope` directly, and only the client layer is exercised by the library
usage documented above.

### Conventions worth keeping

- Talk to a database only through a `Connector`; don't import `clickhouse-connect` or the `neo4j`
  driver directly outside `scope/connectors`.
- Raise an `SCOPEError` subclass (or let `DatabaseError` propagate out of `dal.py`) instead of raising
  `HTTPException` from an endpoint — the centralized handler in `scope/api/__init__.py` is what turns
  it into a response.
- Run `uv run ruff check` before opening a PR.

### CI

A PR touching `api/**` triggers `api-verify.yaml` (a Docker build test — no push) and
`api-test-e2e.yaml` (the `pytest` suite above, against a live Neo4j/ClickHouse hosted in a real cluster). The image is only
actually built and pushed by `api-main.yaml`, which runs on `main` after the E2E workflow succeeds
there.
Check the list of active tests in the CI workflow: (.github/workflows/api-test-e2e.yaml)
