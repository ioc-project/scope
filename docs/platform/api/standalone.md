# Run the API standalone

The SCOPE API is a single service. You can run it by itself, without the workers, the UI or the rest of the
stack, and point it at the databases that hold your data. This page shows what it needs, how to configure it
with a `.env` file, and two ways to run it: from source, or in a container.

!!! tip "Looking for the whole stack?"
    To deploy SCOPE end to end, with the collector, the databases and the workers, see
    [Deploy SCOPE](../../tutorials/deploy-scope.md). This page is for running the API on its own.

## What it needs

| Dependency | Needed for | Required |
|---|---|---|
| **ClickHouse** | The raw OpenTelemetry spans, with the `otel_traces` table | Yes, for the span endpoints |
| **Neo4j** | The knowledge graph: sessions, trajectories, metrics and insights | Yes, for most other endpoints |
| **Redis** | A cache for the slowest queries | No. Without it, the API logs a warning and serves every request from the databases |
| **Python 3.13+** and [`uv`](https://docs.astral.sh/uv/) | Running from source | Only for the source option |
| **Docker** | Running the container | Only for the container option |

The API starts even when a database is unreachable, and the Swagger pages at `/docs` work regardless. Requests to
endpoints that need the missing database fail with an error. A ClickHouse or Neo4j that cannot be reached is
logged at startup, for example `could not ensure trace_labels table`.

## Configure it with a `.env` file

The API reads its settings from environment variables. When it starts from source, it also loads a **`.env`
file** at `backend/api/.env`. Variables already set in the environment take precedence over the file.

Start from the template, which lists the database settings:

```bash
cd backend/api
cp .env.example .env
```

The template only has the ClickHouse and Neo4j variables, and empty. The complete list of variables follows.

### Databases

| Variable | Default | Description |
|---|---|---|
| `SCOPE_LOCAL` | `true` | Set it to **`false`**. With `true`, the library falls back on a local SQLite file for the clients it builds itself. The deployments of SCOPE set it to `false` |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `8123` | ClickHouse **HTTP** port |
| `CLICKHOUSE_USERNAME` | `admin` | ClickHouse user |
| `CLICKHOUSE_PASSWORD` | `admin` | ClickHouse password |
| `CLICKHOUSE_DATABASE` | `default` | Database that holds `otel_traces` |
| `NEO4J_HOST` | `localhost` | Neo4j host. `NEO4J_URI` (for example `bolt://neo4j:7687`) can be given instead, and wins over `NEO4J_HOST` |
| `NEO4J_PORT` | `7687` | Neo4j Bolt port |
| `NEO4J_AUTH` | none | Credentials as `user/password`. When set, it wins over the two variables below |
| `NEO4J_USERNAME` | `neo4j` | Neo4j user, when `NEO4J_AUTH` is not set |
| `NEO4J_PASSWORD` | empty | Neo4j password, when `NEO4J_AUTH` is not set |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database |

### Server

| Variable | Default | Description |
|---|---|---|
| `SCOPE_API_PORT` | `8000` | Port the server listens on (on all interfaces) |
| `SCOPE_API_RELOAD` | `true` | Restart the server when the code changes. This is for development: set it to `false` anywhere else |

### Cache (optional)

| Variable | Default | Description |
|---|---|---|
| `CACHE_ENABLED` | `true` | Set it to `false` to skip Redis completely |
| `CACHE_REDIS_URL` | `redis://localhost:6379/0` | Redis to use |
| `CACHE_TTL_SECONDS` | `55` | How long a cached answer is served |

### Metrics computed on demand (optional)

Only the endpoints that **compute** metrics when called need these. They run LLM-judged metrics, so they need a
model, as the workers do.

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | none | API key of the judge model |
| `OPENAI_BASE_URL` | none | Endpoint of an OpenAI-compatible gateway. `OPENAI_API_BASE` and `LITELLM_BASE_URL` are accepted too |
| `MCE_LLM_MODEL` | `LLM_MODEL_NAME`, then `gpt-4o` | Judge model |
| `MCE_ENGINE_MAX_WORKERS` | `8` | Metrics computed in parallel |

### An example

For a ClickHouse and a Neo4j that run on your machine:

```bash
# backend/api/.env
SCOPE_LOCAL=false

CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USERNAME=scope
CLICKHOUSE_PASSWORD=change-me
CLICKHOUSE_DATABASE=default

NEO4J_HOST=localhost
NEO4J_PORT=7687
NEO4J_AUTH=neo4j/change-me

SCOPE_API_PORT=8000
SCOPE_API_RELOAD=false

# No Redis available: skip the cache
CACHE_ENABLED=false
```

## Option A: run it from source

```bash
cd backend/api
uv sync
cp .env.example .env     # then edit it, see above
uv run dev
```

The server starts on <http://127.0.0.1:8000>. Check it:

```bash
curl http://127.0.0.1:8000/healthz
```

Then open the interactive documentation at <http://127.0.0.1:8000/docs>: see the
[API page](index.md#interactive-api-reference-swagger).

## Option B: run it in a container

The image is built from the `backend/` directory, with the ontology package as an additional build context. From
the root of the repository:

```bash
docker build -f backend/api/Dockerfile --build-context ontology=ontology -t scope-api backend
```

Then run it with your `.env` file:

```bash
docker run --rm -p 8000:8000 --env-file backend/api/.env scope-api
```

Two things differ from running from source:

- **`--env-file` does not parse the file like a shell.** Write `KEY=value` lines, with no `export` and no quotes
  around the values.
- **`localhost` is the container itself.** To reach databases on your machine, use `host.docker.internal` as the
  host (`CLICKHOUSE_HOST`, `NEO4J_HOST`, `CACHE_REDIS_URL`). On Linux, add `--add-host=host.docker.internal:host-gateway`
  to the `docker run` command. Databases in other containers are reached by their name on a shared Docker network.

For example, `backend/api/.env` for databases on the host:

```bash
SCOPE_LOCAL=false
CLICKHOUSE_HOST=host.docker.internal
CLICKHOUSE_USERNAME=scope
CLICKHOUSE_PASSWORD=change-me
NEO4J_HOST=host.docker.internal
NEO4J_AUTH=neo4j/change-me
CACHE_ENABLED=false
```

The server listens on port 8000 inside the container. Change the left-hand side of `-p` to publish it on another
port of your machine, and leave `SCOPE_API_PORT` alone.

## If something does not work

| What you see | Why |
|---|---|
| `could not ensure trace_labels table` in the log at startup | ClickHouse cannot be reached with `CLICKHOUSE_*`. Check the host, the **HTTP** port (8123, not 9000) and the credentials |
| Requests fail with a database error, but `/docs` works | The API is running, and a database is unreachable or empty: check the variables above |
| `Redis is NOT reachable ... caching will be disabled` | A warning only. Set `CACHE_ENABLED=false` to silence it, or point `CACHE_REDIS_URL` to a Redis |
| Neo4j authentication errors | `NEO4J_AUTH` (or the username and password) does not match the database |
| The server restarts by itself | `SCOPE_API_RELOAD` is `true`. Set it to `false` |
| In Docker, the databases are unreachable | A host of `localhost` points at the container: use `host.docker.internal` |

## Related

- [API](index.md): the architecture, using it as a library, and its tests
- [Run the UI standalone](../ui/standalone.md): a dashboard for this API
- [Deploy SCOPE](../../tutorials/deploy-scope.md): the whole stack
