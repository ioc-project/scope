# AGENTS.md

SCOPE analyses multi-agent system (MAS) executions from OpenTelemetry traces and serves the
result through an API and a dashboard. This file is for coding agents asked to **install or
run SCOPE**, to **send it traces**, or to **work on this repository**. Human-oriented docs:
[README](README.md) and <https://ioc-project.github.io/scope/>.

## Rules that save you a wasted run

1. **Instrument applications with the AGNTCY Observe SDK, never a hand-written OpenTelemetry
   exporter.** SCOPE builds its knowledge graph only from spans named `session.start`,
   `session.end`, `*.graph`, `*.agent`, `*.chat` and `*.tool` that carry `ioa_observe.*`
   attributes, and silently skips every other span. A raw exporter appears to work and
   produces nothing in SCOPE. Instructions to paste into the application's own
   `AGENTS.md`: [Instructions for coding agents](docs/tutorials/instrument-your-app.md#instructions-for-coding-agents).
2. **Do not install SCOPE inside the agent application.** SCOPE is a separate deployment. The
   application only emits spans to its OTLP collector.
3. **Do not report success until a session is visible in SCOPE** (see [Verify](#verify)).
   Spans reaching ClickHouse is not enough.

## Install and run

Pick one. Both need an OpenAI-compatible LLM endpoint and a text-embedding model: the analysis
workers call them. The judge workers (cognitive failures, cognition protocols) reason over whole
traces, and the cognitive-failure worker needs a model that supports tool calling.

### Docker Compose (one machine)

Needs Docker with Compose v2.17+ and BuildKit, **6 GB** of memory given to Docker, and a checkout
of the whole repository.

```bash
cd deploy/docker-compose
cp .env.template .env
# edit .env: LLM_MODEL_NAME, EMBEDDING_MODEL_NAME, LLM_GATEWAY_BASE_URL, LLM_GATEWAY_API_KEY,
# and replace every change-me password
docker compose up -d --build
```

- `--build` builds the images from the checkout. Without it Compose pulls published images.
- The dashboard and API are at <http://localhost:8080>. The OTLP collector listens on
  `localhost:4318` (HTTP) and `localhost:4317` (gRPC). Neo4j browser: <http://localhost:7474>.
- `docker compose down -v` deletes all data.

Details: [deploy/docker-compose/README.md](deploy/docker-compose/README.md).

### Kubernetes (Helm)

Run from `deploy/k8s`. Needs Helm 3, `kubectl`, and a GHCR image pull secret (a GitHub token with
`read:packages`) referenced by `global.imagePullSecrets`.

```bash
helm dependency build charts/scope
helm upgrade -i -n scope scope charts/scope --create-namespace
kubectl -n scope port-forward svc/scope-gateway 8080:8080   # then open http://localhost:8080
```

- The LLM credentials, ClickHouse, RabbitMQ and Neo4j each take an inline value or an existing
  Secret. Use existing Secrets for anything shared. Details and the secret layout:
  [deploy/k8s/charts/scope/README.md](deploy/k8s/charts/scope/README.md).
- The `cognitive-failure` and `cognition-protocols` workers are **enabled by default**. The first
  makes a judge-model call for every session. Disable with
  `--set workers.cognitive-failure.enabled=false` or `--set workers.cognition-protocols.enabled=false`.
- The chart ships an OTel collector and its ConfigMap (`templates/otel-collector-configmap.yaml`).
  Check it before writing your own.
- Neo4j data is persistent. If workers fail with `Neo.ClientError.Security.Unauthorized`, the
  existing volume holds an older password: supply a Secret that matches it, or reset the data.

## Send traces

Follow [Instrument your app](docs/tutorials/instrument-your-app.md). Point the SDK at the collector
with `OTLP_HTTP_ENDPOINT` (Compose: `http://localhost:4318`). A runnable example:

```bash
cd examples/currency-demo
OTLP_HTTP_ENDPOINT=http://localhost:4318 uv run main.py 100
```

## Verify

1. Open the dashboard, then **Applications**, then your application, then **Inspect**. The session
   appears a few seconds after the run ends.
2. If spans are in ClickHouse but SCOPE shows no session, check the session-completion
   notification path before changing the instrumentation: the `session-end-trigger` service
   publishes a finished session to the `norm` worker.
3. List the span names exported for a test request. They must include `session.start` and names
   ending in `.agent` and `.tool` (query in the instrumentation tutorial). If they do not, the
   SDK is not in use.
4. Zero cognitive failures does not prove detection works: it can also mean healthy traffic,
   missing fields, or a pattern the detectors do not cover. Test detection with a trace known to
   contain a failure.

## Working on this repository

| Path | Contents |
|---|---|
| `backend/` | API, normalization (`norm`), workers, MCE |
| `ui/` | The dashboard (React, Vite). Built with **yarn**: `yarn.lock` is authoritative, `package-lock.json` is out of date |
| `deploy/` | Docker Compose stack and Helm charts |
| `docs/`, `mkdocs.yml` | The documentation site |
| `ontology/` | The MAS ontology |
| `examples/` | Instrumented sample applications |

- UI against a running stack: in `ui/`, set `VITE_REST_API_URL=http://localhost:8080/api/v1` in
  `.env.local`, then `yarn install` and `yarn dev` (serves on port 3000).
- Keep `deploy/docker-compose` and `deploy/k8s/charts/scope` in sync: the Compose README maps
  each service to its chart equivalent.
- Contribution rules: [CONTRIBUTING.md](CONTRIBUTING.md).
