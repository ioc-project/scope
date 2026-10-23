# SCOPE — Tutorial: Deploy SCOPE

<p class="eyebrow">Running the full SCOPE stack with Docker Compose or Kubernetes</p>

## Before starting

This tutorial deploys SCOPE on its own: the telemetry path, the ingestion and
analysis workers, the API and the dashboard. The agent application is not part of
the deployment. It only needs to export OpenTelemetry spans to the collector, as
described in [Instrument your app](instrument-your-app.md).

SCOPE ships as one image per component, published under
`ghcr.io/ioc-project/container-images/`:

| Component | Image |
|---|---|
| API | `scope-api` |
| Dashboard | `scope-ui` |
| Workers | `scope-norm-worker`, `scope-embedding-worker`, `scope-mce-worker`, `scope-grouping-worker`, `scope-hierarchical-grouping-worker`, `scope-analysis-worker`, `scope-intelligence-worker`, `scope-stateful-eval-worker`, `scope-cognitive-failure-worker`, `scope-cognition-protocols-worker` (not published by CI yet) |

The deployment also needs four pieces of infrastructure: an OTel collector, a span
datastore (ClickHouse), a message broker (RabbitMQ) and the knowledge graph store
(Neo4j). Both deployment options below install them alongside SCOPE.

**What you need for either option:**

- An LLM gateway with an API key. The embedding, grouping, metric and
  stateful-evaluation workers call it. Without a key, spans are still collected and
  normalised into the knowledge graph, but those analyses do not run.
- Memory: about 6 GiB of free memory for the default Kubernetes install, and 12 GB
  given to Docker for Compose.

Pick one of the two options.

## Option A: Docker Compose

Best for a laptop or a single VM. Docker with the Compose plugin (v2.17 or later) is
the only prerequisite, and Docker should have at least **12 GB of memory**.

### 1. Clone SCOPE repo

```bash
git clone https://github.com/ioc-project/scope.git
cd scope/deploy/docker-compose
```

### 2. Configure the environment

```bash
cp .env.template .env
```

Edit `.env`: replace the `change-me` passwords, and set your LLM gateway details.

??? example ".env (the settings you will most likely change)"
    ```bash
    LLM_MODEL_NAME=gpt-4o
    EMBEDDING_MODEL_NAME=text-embedding-3-small
    LLM_GATEWAY_BASE_URL=https://api.openai.com/v1
    LLM_GATEWAY_API_KEY=

    CLICKHOUSE_PASSWORD=change-me
    RABBITMQ_PASSWORD=change-me
    NEO4J_PASSWORD=change-me

    SCOPE_PORT=8080   # host port of the dashboard and the API
    ```

`LLM_MODEL_NAME` and `EMBEDDING_MODEL_NAME` are required: Compose refuses to start
without them. The template documents every other setting (ports, UI feature flags,
image tag).

### 3. Start the stack

```bash
docker compose up -d --build
docker compose ps
```

`--build` builds the images from the checkout. Without it, Compose pulls
`ghcr.io/ioc-project/container-images/scope-*:${SCOPE_VERSION}`, so those tags must
exist in the registry. The first build takes a while.

All services should reach `running` (and `healthy` where a healthcheck exists), and
`clickhouse-setup` should show `exited (0)`: it is a one-shot job.

### 4. Check the pipeline with a test span

Send one `session.end` span to the collector. It is enough to walk the whole
telemetry path:

```bash
NOW=$(python3 -c "import time; print(int(time.time()*1e9))")
curl -s -X POST localhost:4318/v1/traces -H 'Content-Type: application/json' -d '{
  "resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "demo-app"}}]},
    "scopeSpans": [{"spans": [{
      "traceId": "5b8efff798038103d269b633813fc60c",
      "spanId": "eee19b7ec3c1b174",
      "name": "session.end", "kind": 1,
      "startTimeUnixNano": "'$NOW'", "endTimeUnixNano": "'$((NOW+1000000))'",
      "attributes": [{"key": "session.id", "value": {"stringValue": "demo_sess-1"}}]
    }]}]
  }]
}'
```

The collector answers `{"partialSuccess":{}}`. After a few seconds, the norm worker
should report the session:

```bash
docker compose logs norm | grep "sess-1"
```

You should see the worker processing `sess-1`, then sending the session on to the
next queues. That confirms the collector, ClickHouse, the session trigger, RabbitMQ
and the ingestion worker are connected.

Then open **http://localhost:8080** (or your `SCOPE_PORT`) for the dashboard.

### 5. Stop or reset

```bash
docker compose down        # stop, keep the data
docker compose down -v     # stop and delete ClickHouse, RabbitMQ and Neo4j data
```

## Option B: Kubernetes

Best for a shared or long-running deployment. SCOPE is installed with the `scope`
umbrella Helm chart, which deploys the API, the dashboard, the workers, the
collector, ClickHouse, RabbitMQ, Neo4j and an internal gateway.

### 1. Prerequisites

- A Kubernetes cluster (a local [kind](https://kind.sigs.k8s.io/) cluster works).
- Helm 3 and `kubectl`, configured for that cluster.
- A node pool with about **6 GiB of allocatable memory** for the default values.
  Most of it is Neo4j, which requests 4 GiB. On a small local cluster, lower it
  with `--set neo4j.neo4j.resources.memory=2Gi` (the chart refuses less than 2 GiB).

### 2. Get the chart

The chart lives in the SCOPE repository:

```bash
git clone https://github.com/ioc-project/scope.git
cd scope/deploy/k8s
helm dependency build charts/scope
```

### 3. Install

Pass your LLM gateway settings at install time. The chart stores them in a Secret
used by the workers:

```bash
helm upgrade -i -n scope scope charts/scope --create-namespace \
  --set secrets.llm.llmGatewayBaseUrl=https://api.openai.com/v1 \
  --set secrets.llm.llmGatewayApiKey=<your-api-key> \
  --set secrets.llm.llmModelName=gpt-4o \
  --set secrets.llm.embeddingModelName=text-embedding-3-small
kubectl get pods -n scope
```

Pods start in dependency order, so give them a few minutes to become ready.

!!! warning "Replace the default credentials"
    The chart ships placeholder passwords for RabbitMQ and Neo4j (`change-me`) so
    that a local install works out of the box. For anything shared, create the
    Secrets yourself and reference them with `rabbitmq.auth.existingSecret` and
    `neo4j.neo4j.passwordFromSecret`. The same pattern exists for ClickHouse and
    the LLM credentials. See the chart's
    [README](https://github.com/ioc-project/scope/blob/main/deploy/k8s/charts/scope/README.md)
    for the details.

!!! note "If Neo4j rejects the password after a reinstall"
    Neo4j data is persistent. A reinstall can keep the password of the previous
    install, and the workers then fail with `Neo.ClientError.Security.Unauthorized`.
    Delete the Neo4j volume and Secret before reinstalling, or provide a Secret
    that matches the existing database.

### 4. Open the dashboard

The chart exposes the dashboard and the API through one internal gateway. Forward
it to your machine:

```bash
kubectl -n scope port-forward svc/scope 8080:8080
```

Open **http://localhost:8080**. For a permanent setup, enable the gateway's
ingress (`gateway.ingress.enabled`) rather than exposing the API and the dashboard
separately, so the browser keeps calling the API on the same origin.

### 5. Reach the collector

The collector's OTLP/HTTP port is cluster-internal. Applications running in the
cluster can export to
`http://scope-otel-collector.scope.svc.cluster.local:4318`. To send spans from your
machine, forward the port:

```bash
kubectl -n scope port-forward svc/scope-otel-collector 4318:4318
```

### 6. Check the pipeline

Send the same test span as in the Compose option, then look at the norm worker:

```bash
kubectl -n scope logs deploy/scope-worker-norm | grep "sess-1"
```

### 7. Uninstall

```bash
helm uninstall -n scope scope
kubectl -n scope delete pvc --all     # also deletes the stored data
```

## Send your own application's traces

Point your instrumented application at the collector's OTLP/HTTP endpoint:

```bash
export OTLP_HTTP_ENDPOINT=http://localhost:4318
```

Run a session, then open the dashboard. A session is ingested once it ends: the SDK emits a
`session.end` span when the `session_start()` context exits, and that span is what
the session trigger watches for. A session that never ends is never analysed. The instrumentation steps are in
[Instrument your app](instrument-your-app.md).

## Related

- [Instrument your app](instrument-your-app.md)
- [Ingestion](../ingestion/index.md): the telemetry path this deployment implements
- [Analysis](../analysis/index.md): what the workers produce
- [Platform](../platform/index.md): what the dashboard shows
