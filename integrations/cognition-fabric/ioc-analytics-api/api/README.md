# Analytics API

Unified data-access layer of OXP (package `oxp-api`, import name `oxp`). It consolidates the data-access patterns, the DAL, ontology-hub and the workflow components into one library, and provides a REST API (FastAPI) on top of it for the downstream components (UI, workers, MCE clients, external consumers).

It talks to three backends:

| Backend | Role | Used for |
|---------|------|----------|
| **ClickHouse** | Raw OTel spans (`otel_traces` table) and session labels (`trace_labels`) | Applications, sessions, spans, timelines, charts, live topology, labels |
| **Neo4j** | Knowledge graph built by `norm` and enriched by the workers | KG state, embeddings and neighbours, metrics, concepts, semantic groups, symbolic models, waste estimation, insights, and the DAL used by the workers |
| **Redis** (optional) | Response cache | Short-lived caching of read endpoints. If Redis is unreachable the API keeps working without cache |

## Tech stack

- **Python 3.13+**
- **FastAPI** + **Uvicorn**: HTTP server
- **clickhouse-connect** / **clickhouse-sqlalchemy** / **SQLAlchemy**: ClickHouse and SQL access
- **neo4j** driver: knowledge graph access
- **redis**: caching
- **Pydantic** (+ `pydantic-settings`): response models and settings
- **ioc-scope-ontology**: local ontology models and KG object verification

## Code layout

```
api/
├── scope/
│   ├── api/                  # FastAPI app (scope.api:app), routers for /api/v1 and /api/v2
│   │   ├── api_v1/endpoints/ # one module per domain (sessions, applications, metrics, kg, ...)
│   │   └── api_v2/endpoints/ # v2 metrics endpoints
│   ├── app.py                # `dev` entry point: starts uvicorn (reads .env)
│   ├── client/               # LocalClient: in-process client, split into domain mixins
│   │   └── mock/             # GoldenClient: static reference responses (no DB needed)
│   ├── connectors/           # ClickHouseConnector, Neo4JConnector, SQLAlchemyConnector
│   ├── query_builders/       # SQL / Cypher builders, one module per domain
│   ├── providers/            # OXPMetricsProvider / OXPKGProvider (Neo4j-backed) used by MCE
│   ├── interfaces/           # ABCs and models for pluggable providers (used by mce-helper)
│   ├── models/               # Pydantic response/request models
│   ├── repositories/         # legacy OtelRepository abstraction (SQLite / ClickHouse)
│   ├── core/                 # settings (config.py) and domain exceptions
│   ├── cache.py              # Redis cache helper
│   └── dependencies.py       # FastAPI dependencies, singleton connectors
├── data/                     # ClickHouse / Postgres / SQLite DDL and sample CSV data
├── deploy/docker-compose.yml # Redis (ClickHouse/API/Postgres services are commented examples)
├── scripts/                  # helper scripts (CSV to SQL, metrics smoke tests)
├── tests/                    # pytest suites (mostly integration tests, see below)
├── Dockerfile
└── Taskfile.yml
```

Every operation follows the same three-step pipeline: **query builder** (`scope.query_builders.<domain>`) → **connector** (`db.execute(...)`) → **Pydantic model** mapping. The REST endpoints are thin wrappers around `LocalClient`; most read endpoints support optional Redis caching, disabled by default.

## Quick start

Run the commands from the [Analytics API workspace](../README.md). The workspace uses local SCOPE MCE and ontology packages.

```bash
# 1. Install dependencies
uv sync --project api

# 2. Configure the backends (see "Configuration"); a .env file in the working directory is also loaded
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USERNAME=admin
export CLICKHOUSE_PASSWORD=admin
export CLICKHOUSE_DATABASE=oxp
export NEO4J_HOST=localhost
export NEO4J_PORT=7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=<password>

# 3. Run the server
uv run --project api dev
```

The API is then available at **http://127.0.0.1:8000** (`OXP_API_PORT` to change the port). Interactive OpenAPI docs are served at **http://127.0.0.1:8000/docs**.

Notes:
- `dev` (`scope.app:main`) starts `uvicorn` on `0.0.0.0` with **auto-reload enabled by default**. Set `OXP_API_RELOAD=false` for anything that is not local development.
- To run uvicorn directly: `uvicorn scope.api:app --host 0.0.0.0 --port 8000`. The ASGI application is `scope.api:app` (not `scope.app:app`).
- The Cognition Fabric worker containers ([`docker-compose.yml`](../../docker-compose.yml)) expect the API at `http://host.docker.internal:8081` (`API_BASE_URL`), so use `OXP_API_PORT=8081` when running the API outside Compose.
- At startup the API creates the `trace_labels` table if needed and pings Redis (logging whether caching is enabled). Neo4j and Redis outages do not prevent startup: Neo4j-backed endpoints (metrics in particular) answer `503` until the database is reachable.

### Database schemas

The DDL for the ClickHouse tables is in [`data/clickhouse/otel_traces.sql`](data/clickhouse/otel_traces.sql) (database `oxp`, table `otel_traces`, the schema produced by the OTel collector's ClickHouse exporter plus `session_id`, `agent_id` and `application_id` columns) and [`data/clickhouse/trace_labels.sql`](data/clickhouse/trace_labels.sql). Postgres/SQLite variants and sample CSVs are under `data/`.

## Configuration

Settings are read from environment variables (and from `.env` by the `dev` entry point; see [`.env.example`](.env.example)).

| Variable | Default | Description |
|----------|---------|-------------|
| `CLICKHOUSE_HOST` / `CLICKHOUSE_PORT` | `localhost` / `8123` | ClickHouse HTTP endpoint |
| `CLICKHOUSE_USERNAME` / `CLICKHOUSE_PASSWORD` | `admin` / `admin` | ClickHouse credentials |
| `CLICKHOUSE_DATABASE` | `default` | ClickHouse database (use `oxp` with the provided DDL) |
| `NEO4J_HOST` | `localhost` | Neo4j host. A `host:port` or a `bolt://host:port` URI is also accepted; `NEO4J_URI` takes precedence when set |
| `NEO4J_PORT` | `7687` | Neo4j Bolt port (used when the host carries no port) |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | `neo4j` / *(empty)* | Neo4j credentials. `NEO4J_AUTH=user/password` (as understood by the Neo4j image) takes precedence |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database |
| `CACHE_REDIS_URL` | `redis://localhost:6379/0` | Redis used for caching |
| `CACHE_ENABLED` | `false` | Set to `true` to enable the Redis cache |
| `CACHE_TTL_SECONDS` | `55` | TTL of the cached responses |
| `OXP_API_PORT` | `8000` | Port used by `dev` |
| `OXP_API_RELOAD` | `true` | Uvicorn auto-reload used by `dev` |
| `OXP_LOCAL` | `true` | Only used by the legacy repository factory, see [Python library](#using-as-a-python-library) |
| `MCE_LLM_MODEL` / `LLM_MODEL_NAME` | `gpt-4o` | Model used by LLM-based metrics when computing metrics in the API |
| `MCE_LLM_MODE` / `MCE_LLM_CACHE_PATH` | `live` / `llm_cache.json` | MCE LLM service mode and cache file |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` (or `OPENAI_API_BASE`, `LITELLM_BASE_URL`) | none | Credentials / gateway for LLM-based metrics |
| `MCE_ENGINE_MAX_WORKERS` | `8` | Worker threads of the `MetricEngine` used by `/metrics` |

The metric categories exposed by the UI endpoints (general, performance, quality, reliability, tools, cost, llm, conversation) and their definitions are declared in [`scope/core/config.py`](scope/core/config.py) (`METRIC_CATEGORIES`).

### Caching

Read endpoints are cached in Redis under the `cache:*` namespace, keyed by endpoint and parameters. Failures of Redis are logged and bypassed. To drop every cached entry:

```bash
curl -X DELETE http://127.0.0.1:8000/api/v1/cache/
```

## REST API

All routes are mounted under `/api/v1` (and `/api/v2` for the metrics endpoints that have a v2). `GET /healthz`, `GET /api/v1/healthz` and `GET /api/v2/healthz` are health probes. Errors are returned as `{"detail": "..."}` (domain errors map to 404 / 422 / 502 / 503, unexpected errors to 500).

| Prefix (under `/api/v1`) | Backend | Main operations |
|--------------------------|---------|-----------------|
| `/ui` | ClickHouse | Application names and application lists (`/ui/applications`, `/ui/applications/names`, `/ui/applications/multiquery`, `/ui/applications-from-clickhouse`) |
| `/applications/{application_id}` | ClickHouse + Neo4j | Application monitoring data; `/agents`, `/sessions`, `/sessions_with_stateful_eval`, `/sessions_with_stateful_eval_multi_query`, `/agent/{agent_id}/charts`, `/topology`, `/semanticgroups`, `/semanticgroups/table`, `/semanticgroups/{id}/impact_assessment` |
| `/sessions` | ClickHouse + Neo4j | `GET /` (list) and `/count`; per session: `/{session_id}/spans`, `/timeline-waterfall`, `/timeline`, `/trajectory`, `/execution-graph`, `/conversation`, `/latent-space`; agent details and impact assessment under `/sessions/sessions/{session_id}/...` |
| `/spans/{span_id}` | ClickHouse | Span details |
| `/kg` | Neo4j | `GET /get_state_by_session_id/{id}`, `POST /neighbors` (embedding similarity), `POST /query` (node query used by `mce-client`'s `OXPApiRawProvider`) |
| `/metrics` | Neo4j + MCE | `/catalog`, `/catalog/{scope}`, `/catalog_with_categories[_meta]`, `/info`, `/timeseries`; `GET` / `POST /sessions/{id}` (read, compute on the fly with `compute`, `persist`, `use_cache`, `hops`, `metric_names`), `GET` / `POST /sessions/{id}/spans/{span_id}`, `POST /compute` (batch), `/applications/{id}/summary`, `/applications/{id}/metrics`, `/applications/{id}/category/{key}` |
| `/concepts` | Neo4j | Neurosymbolic concepts: `GET` / `POST /{app_id}` and per-span variants |
| `/semanticgroups/{id}` | Neo4j | Details, `/normal_behavior`, `/consistency_report`, `/anomaly_report` |
| `/symbolic` | Neo4j | Symbolic variables, models and session values/state (`/applications/{id}/variables`, `/applications/{id}/models[/{model_id}]`, `/sessions/{id}/values`, `/sessions/{id}/state`) |
| `/session-reasoning-path`, `/application-reasoning-path`, `/timeline-reasoning-path`, `/applications-with-stateful-eval` | Neo4j | Reasoning paths and stateful-evaluation views (no prefix) |
| `/waste-estimation` | Neo4j | Waste estimations, top wasteful sessions, cost-efficiency groups, wasted-resources scores, semantic-group and session insights |
| `/live_topology` | ClickHouse | `/{application_name}/sessions`, `/agents`, `/tools`, `/sessions/{session_id}/topology` |
| `/labels/{session_id}` | ClickHouse | `GET` / `PUT` / `DELETE` of a session label |
| `/cognition-store` | PostgreSQL | Generic JSON items (`POST` / `GET /store`), subscription callbacks (`POST` / `GET /subscriptions`, `DELETE /subscriptions/{id}`), diagnostics (`GET /info`), and legacy findings. See [Cognition Store API](../../docs/cognition_store.md) and the [OpenAPI spec](specs/oxp_openapi.yaml). |
| `/agentic-protocols-metrics` | ClickHouse | Agentic-protocol metrics for an application (no prefix) |
| `/cache` | Redis | `DELETE /` flushes the cache |
| `/features/info` (and `/info` under `/ui`, `/kg`, `/metrics`) | | Descriptive `info` endpoints |

| Prefix (under `/api/v2`) | Description |
|--------------------------|-------------|
| `/metrics/applications/{application_id}/metrics`, `/metrics/info` | Explicit-metric-list variant of the application metrics endpoint |

The OpenAPI document served at `/docs` is the reference for parameters and response schemas. A manual test guide is available in [`docs/api-testing.md`](docs/api-testing.md) (its examples use a local dev instance and port 8001).

### Known limitations

- There is **no authentication or authorization**: deploy the API behind a trusted network or gateway.
- Some query builders (e.g. labels, `POST /kg/query`) build queries by string interpolation from request parameters. Do not expose the API to untrusted callers.
- `/metrics` computation uses SCOPE's MCE packages. LLM-based metrics need the `OPENAI_*` / `MCE_LLM_*` settings.

## Docker

Build the image from the Analytics API workspace with the SCOPE source directories as additional BuildKit contexts:

```bash
docker buildx build --load -f api/Dockerfile \
  --build-context scope_backend=../../../backend \
  --build-context ontology=../../../ontology \
  -t oxp-api .
```

Run it standalone (the container starts `dev` on port 8000; disable the reload for containers):

```bash
docker run --rm -p 8000:8000 \
  -e OXP_API_RELOAD=false \
  -e CLICKHOUSE_HOST=host.docker.internal \
  -e CLICKHOUSE_PORT=8123 \
  -e CLICKHOUSE_USERNAME=admin \
  -e CLICKHOUSE_PASSWORD=admin \
  -e CLICKHOUSE_DATABASE=oxp \
  -e NEO4J_HOST=host.docker.internal \
  -e NEO4J_PASSWORD=<password> \
  -e CACHE_REDIS_URL=redis://host.docker.internal:6379/0 \
  oxp-api
```

[`deploy/docker-compose.yml`](deploy/docker-compose.yml) currently starts only a Redis instance (`redis:7-alpine`, port 6379), which is enough for local development and for the tests; the ClickHouse, API and Postgres services in this file are commented-out examples. ClickHouse can be initialised with the DDL in `data/clickhouse/`.

CI builds the image on pull requests (`api-verify.yaml`) and publishes it from `main` (`api-main.yaml`).

## Running tests

```bash
# From the repository root
uv sync --project api --group dev
uv run --project api --group dev --no-sync python -m pytest api/tests/<test_file>.py -v
```

Most suites in [`tests/`](tests) are **integration tests** against live backends: they need ClickHouse and/or Neo4j populated with data (connection settings from the environment or a `.env` file, e.g. `NEO4J_PASSWORD`) and use `fakeredis` instead of a real Redis. Suites that mock or stub the backends (for example `test_live_topology.py`, `test_local_client_lifecycle.py`, `test_*_mocks.py`) run without infrastructure. CI runs the integration list one file at a time through the manually triggered `api-test-e2e.yaml` workflow.

[`Taskfile.yml`](Taskfile.yml) provides smoke tests against a running server, e.g. `task test-single` and `task test-sequence` (`API_PORT`, `SESSION_ID`, `METRIC_ID` and `COMPUTE_METRIC_ID` can be overridden).

Format and lint with Ruff: `uv run ruff format api && uv run ruff check --fix api`.

## Using as a Python library

Besides running as a REST server, `oxp-api` can be used as a library by the other packages of the workspace (it is how the workers and `workflows` access the DBs) or installed on its own. Inside the workspace, depend on it with `oxp-api = { workspace = true }` in `[tool.uv.sources]`. Outside, install it from the repository subdirectory:

```bash
pip install "git+https://github.com/cisco-eti/oxp-lib.git#subdirectory=api"
```

### Quick example

`LocalClient` queries the databases in-process, without going through HTTP. Pass the connector(s) explicitly, or build the client from the API settings (environment variables listed above):

```python
from scope import LocalClient

# ClickHouse for spans, same settings as the REST server
with LocalClient.from_settings() as client:
    apps = client.get_application_names()
    sessions = client.list_sessions(limit=10)
```

`LocalClient.from_settings(persist_metrics=True)` also opens a Neo4j connection and persists metrics through it. Connections opened by `from_settings()` are closed by `close()` / the context manager; connectors that you inject remain yours to close.

### Specifying database credentials

`LocalClient(db=...)` takes a **Connector** instance. All connectors share the interface of `scope.connectors.base.Connector` (`connect()`, `close()`, `is_connected()`, `execute()`, `execute_command()`, and context-manager support). The SQL dialect (ClickHouse, SQLite or Neo4j/Cypher) is inferred from the connector type.

#### Option A: ClickHouse (via `clickhouse-connect`)

```python
from scope import LocalClient
from scope.connectors.clickhouse import ClickHouseConnector

db = ClickHouseConnector(
    host="clickhouse.example.com",
    port=8123,
    username="oxp_user",
    password="<password>",
    database="oxp",
)

with LocalClient(db=db) as client:
    apps = client.get_applications()
```

#### Option B: Neo4j (for KG, metrics, concepts, symbolic and DAL operations)

The knowledge-graph operations require a Neo4j-backed connector:

```python
from scope import LocalClient
from scope.connectors.neo4j import Neo4JConnector

db = Neo4JConnector(
    host="neo4j.example.com",
    port=7687,
    username="neo4j",
    password="<password>",
    database="neo4j",
)

with LocalClient(db=db) as client:
    state = client.get_state_by_session_id("session-abc")
    metrics = client.get_session_metrics("session-abc", hops=2)
    concepts = client.get_app_concepts("my-app")
```

### Available client methods

`LocalClient` is composed of domain mixins; the main public methods are listed below (the full list is in `scope/client/_*.py`). Most keyword arguments are keyword-only.

| Domain | Methods |
|--------|---------|
| **Applications** | `get_application_names`, `get_applications` (+ `_multiquery`, `_from_clickhouse` variants), `get_application_details`, `get_application_agents`, `get_application_sessions`, `get_application_sessions_with_stateful_eval[_multi_query]`, `get_application_charts`, `get_application_topology`, `get_agentic_protocols_metrics`, `get_application_semanticgroups[_table]`, `get_application_semanticgroup_impact_assessment` |
| **Sessions** | `get_sessions`, `list_sessions`, `iter_session_spans`, `get_session_spans`, `get_session_agents`, `get_session_agent_details`, `get_session_timeline[_waterfall]`, `get_session_trajectory`, `get_session_execution_graph`, `get_session_conversation`, `get_session_latent_space`, `get_session_impact_assessment`, `get_sessions_count` |
| **Spans** | `get_spans(span_id=...)` |
| **KG** | `get_state_by_session_id`, `get_embedding_by_session_id`, `get_neighbors` |
| **Metrics** | `get_session_metrics`, `get_span_metrics`, `get_metrics`, `get_metrics_per_category`, `get_application_metrics_summary`, `write_session_metrics`, `write_span_metrics` |
| **Concepts** | `get_app_concepts`, `get_span_concepts`, `write_app_concepts`, `write_span_concepts` |
| **Semantic groups** | `get_semanticgroup_details`, `get_semanticgroup_normal_behavior`, `get_semanticgroup_consistency_report`, `get_semanticgroup_anomaly_report` |
| **Symbolic / reasoning** | `get_application_symbolic_models`, `write_application_symbolic_model`, `get_application_symbolic_variables`, `write_application_symbolic_variables`, `write_session_symbolic_values`, `get_session_symbolic_values`, `get_session_symbolic_state`, `get_session_reasoning_path`, `get_timeline_reasoning_path`, `get_application_reasoning_path`, `get_applications_with_stateful_eval` |
| **Labels** | `get_label`, `put_label`, `delete_label` |
| **Live topology** | `get_live_topology_sessions`, `get_live_agents`, `get_live_tools`, `get_realtime_topology` |

The DAL helpers used by the workers (ingestion of the normalized KG, embeddings, semantic groups, consistency/anomaly/normal-behaviour reports, insights, impact-assessment and waste-estimation data, hierarchical-grouping locks, ...) are functions in [`scope/client/dal.py`](scope/client/dal.py) that take a `Connector`; `worker-base` wraps them in `OXPApiDALAdapter`.

### Type hints & return types

Methods return **Pydantic models** (e.g. `ApplicationsResponse`, `SpanDetailsResponse`) defined in `scope.models.otel_traces`, `scope.models.kg` and `scope.models.live_topology`, or plain dicts for graph-shaped payloads. Models are serializable:

```python
from scope import LocalClient

with LocalClient.from_settings() as client:
    resp = client.get_applications()
    for app in resp.applications:
        print(app.applicationName, app.cost)

    print(resp.model_dump_json(indent=2))
```

For dependency injection or testing, type-hint against the abstract base class, or use `GoldenClient`, which returns static reference data without any database:

```python
from scope import OXPAPIClient
from scope.client import GoldenClient


def analyze(client: OXPAPIClient):
    ...


analyze(GoldenClient())
```

### Extending: providers

`scope.interfaces` defines the ABCs (`MetricsProvider`, `KGProvider`, `DataProvider`) consumed by `mce-helper`/`mce-client`; `scope.providers` contains the Neo4j-backed implementations (`OXPMetricsProvider`, `OXPKGProvider`). `LocalClient(metrics_provider=...)` lets you inject another metrics provider.